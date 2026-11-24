package au.lachlan.musichunter

import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.webkit.JavascriptInterface
import android.webkit.WebView
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.ForwardingPlayer
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.HttpDataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.CacheWriter
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.exoplayer.upstream.DefaultLoadErrorHandlingPolicy
import androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy
import org.json.JSONObject

/**
 * The native player behind the one-page app (SPEC.md §20.3; web/src/core/native.ts). The page says what plays now
 * (`load`) and what's next (`setNext`); the player goes straight on to the next song when one ends, without waiting
 * for the page (with the screen off the page can be slow to hear about it, #72), and tells the page, which confirms
 * it. Songs the page expects next are fetched ahead into a cache on the phone, so they start at once.
 *
 * Each song is queued with a stretch of silence behind it: if nothing is next, the player moves on to the silence and
 * stays "playing" while the page asks the Pi what comes next. A player that had stopped could lose its place in the
 * foreground, and Android doesn't let an app in the background get it back.
 */
@UnstableApi
class AudioBridge(private val app: App) {
    private val main = Handler(Looper.getMainLooper())

    /** Songs fetched ahead (and everything streamed), kept on the phone; emptied at each start, since a song's file on
     *  the Pi can change under the same address (a better copy, an extended mix). */
    private val cache: SimpleCache = java.io.File(app.cacheDir, "audio").let { dir ->
        dir.deleteRecursively()
        SimpleCache(dir, LeastRecentlyUsedCacheEvictor(400L * 1024 * 1024), StandaloneDatabaseProvider(app))
    }
    private val cached = CacheDataSource.Factory().setCache(cache)
        .setUpstreamDataSourceFactory(DefaultHttpDataSource.Factory().setAllowCrossProtocolRedirects(true))
        .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
    private val preloader = java.util.concurrent.Executors.newSingleThreadExecutor()
    private val preloading = HashMap<String, CacheWriter>()

    val exo: ExoPlayer = ExoPlayer.Builder(app)
        .setAudioAttributes(
            AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(), true,
        )
        .setHandleAudioBecomingNoisy(true)
        .setWakeMode(C.WAKE_MODE_NETWORK)
        .setMediaSourceFactory(DefaultMediaSourceFactory(DefaultDataSource.Factory(app, cached))   // http: through the cache
            .setLoadErrorHandlingPolicy(NetworkRetries()))
        .build()

    /** The page; it hears the player's events. */
    var web: WebView? = null
    private var metadata = MediaMetadata.EMPTY
    private var announced = false          // loaded sent for this song
    private var endedSent = false
    private var nextUrl: String? = null    // what the page says comes next (queued behind the song)
    private var nextMeta: MediaMetadata = MediaMetadata.EMPTY
    var pageVisible = true

    /** Find music's − / + for the notification: null when the music isn't Find music, else 0 (neither chosen), 1 (−)
     *  or 3 (+). */
    var votes: Int? = null
        private set
    var onVotes: () -> Unit = {}
    fun setVotes(v: Int?) {
        if (votes == v) return
        votes = v
        onVotes()
    }

    val hasTrack get() = exo.mediaItemCount > 0

    /** What the notification and lock screen control: next, previous and − / + go to the page. */
    val sessionPlayer: Player = object : ForwardingPlayer(exo) {
        private val extra = listOf(
            Player.COMMAND_SEEK_TO_NEXT, Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM,
            Player.COMMAND_SEEK_TO_PREVIOUS, Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM,
        )
        override fun getAvailableCommands(): Player.Commands =
            super.getAvailableCommands().buildUpon().addAll(*extra.toIntArray()).build()
        override fun isCommandAvailable(command: Int) = command in extra || super.isCommandAvailable(command)
        override fun hasNextMediaItem() = true
        override fun hasPreviousMediaItem() = true
        override fun seekToNext() = action("nexttrack")
        override fun seekToNextMediaItem() = action("nexttrack")
        override fun seekToPrevious() = action("previoustrack")
        override fun seekToPreviousMediaItem() = action("previoustrack")
        override fun play() { app.connectService(); if (onSilence()) { endedSent = false; exo.seekTo(0, 0) }; super.play() }
    }

    init {
        exo.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(state: Int) {
                if (onSilence()) return
                when (state) {
                    Player.STATE_READY -> announce()
                    Player.STATE_BUFFERING -> if (exo.playWhenReady) emit("waiting")
                }
            }
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (onSilence()) return
                if (isPlaying) { announce(); emit("playing") }
                else if (!exo.playWhenReady) emit("pause")
            }
            override fun onMediaItemTransition(item: MediaItem?, reason: Int) {
                if (item?.mediaId == SILENCE && !endedSent) { endedSent = true; emit("ended") }
                if (item?.mediaId == NEXT) advance()
            }
            override fun onPlayerError(error: PlaybackException) {
                // (the server's answer too, so the logs say why: feedback #127, "ERROR_CODE_IO_BAD_HTTP_STATUS")
                var c: Throwable? = error.cause
                var detail = ""
                while (c != null) {
                    if (c is androidx.media3.datasource.HttpDataSource.InvalidResponseCodeException) {
                        detail = " ${c.responseCode} ${c.dataSpec.uri.path} @${c.dataSpec.position}"
                        break
                    }
                    c = c.cause
                }
                js("error", JSONObject().put("message", error.errorCodeName + detail))
            }
        })
        tick()
    }

    private fun onSilence() = exo.currentMediaItem?.mediaId == SILENCE

    /** The song ended and the player went on to the next one by itself: the page is told, and confirms it. */
    private fun advance() {
        val url = nextUrl ?: return
        nextUrl = null
        metadata = nextMeta
        main.post {
            if (exo.currentMediaItemIndex > 0) exo.removeMediaItems(0, exo.currentMediaItemIndex)
            val last = exo.mediaItemCount - 1
            if (last >= 0 && exo.getMediaItemAt(last).mediaId == SILENCE)
                exo.replaceMediaItem(last, exo.getMediaItemAt(last).buildUpon().setMediaMetadata(metadata).build())
        }
        announced = false; endedSent = false
        js("advanced", JSONObject().put("url", url))
    }

    /** Fetch a song into the phone's cache now. */
    fun preload(url: String) {
        if (!url.startsWith("http") || app.offline.localAudio(url) != null) return
        synchronized(preloading) {
            if (url in preloading) return
            while (preloading.size >= 2) preloading.remove(preloading.keys.first())?.cancel()   // the older ones
        }
        preloader.execute {
            val w = CacheWriter(cached.createDataSourceForDownloading(), DataSpec(Uri.parse(url)), null, null)
            synchronized(preloading) { preloading[url] = w }
            try { w.cache() } catch (e: Exception) { /* only a speed-up */ }
            finally { synchronized(preloading) { if (preloading[url] === w) preloading.remove(url) } }
        }
    }

    private fun announce() {
        if (!announced && exo.duration != C.TIME_UNSET) { announced = true; emit("loaded") }
    }

    /** Position updates; less often with the screen off, since nobody's watching. */
    private fun tick() {
        if (exo.isPlaying && !onSilence()) emit("time")
        main.postDelayed({ tick() }, if (pageVisible) 250 else 1000)
    }

    private fun state(): JSONObject = JSONObject().apply {
        if (!onSilence()) {
            put("t", exo.currentPosition / 1000.0)
            if (exo.duration != C.TIME_UNSET) put("d", exo.duration / 1000.0)
        }
    }

    private fun emit(type: String) = js(type, state())
    fun action(name: String) = js("action", JSONObject().put("name", name))
    private fun js(type: String, data: JSONObject) {
        main.post { web?.evaluateJavascript("window.__mhNative&&__mhNative('$type',$data)", null) }
    }

    private fun uriFor(url: String): Uri =
        app.offline.localAudio(url)?.let { Uri.fromFile(it) } ?: Uri.parse(url)              // downloaded: from the phone

    private fun meta(title: String, artist: String, art: String) = MediaMetadata.Builder().setTitle(title).setArtist(artist)
        .setArtworkUri(if (art.isEmpty()) null else Uri.parse(art)).build()

    // --- what the page calls (through PageBridge, on the main thread) ----------------------------------------------

    fun load(url: String, atMs: Long, title: String, artist: String, art: String) {
        metadata = meta(title, artist, art)
        nextUrl = null
        announced = false; endedSent = false
        val track = MediaItem.Builder().setMediaId("track").setUri(uriFor(url)).setMediaMetadata(metadata).build()
        val silence = MediaItem.Builder().setMediaId(SILENCE).setUri("asset:///silence.ogg").setMediaMetadata(metadata).build()
        exo.setMediaItems(listOf(track, silence), 0, atMs)
        exo.prepare()
    }

    /** What comes after the song playing ("" for nothing): queued behind it, and fetched into the cache. */
    fun setNext(url: String, title: String, artist: String, art: String) {
        if (!hasTrack) return
        val m = meta(title, artist, art)
        val i = (exo.currentMediaItemIndex + 1 until exo.mediaItemCount).firstOrNull { exo.getMediaItemAt(it).mediaId == NEXT }
        if (url.isEmpty()) { if (i != null) exo.removeMediaItem(i); nextUrl = null; return }
        if (onSilence()) return                                 // too late: the page is already moving on
        if (nextUrl == url && i != null) { nextMeta = m; exo.replaceMediaItem(i, exo.getMediaItemAt(i).buildUpon().setMediaMetadata(m).build()); return }
        nextUrl = url; nextMeta = m
        val item = MediaItem.Builder().setMediaId(NEXT).setUri(uriFor(url)).setMediaMetadata(m).build()
        if (i != null) exo.replaceMediaItem(i, item) else exo.addMediaItem(exo.currentMediaItemIndex + 1, item)
        preload(url)
    }

    fun play() {
        app.connectService()
        if (onSilence()) { endedSent = false; exo.seekTo(0, 0) }   // played again after it ended
        exo.play()
    }

    fun pause() = exo.pause()

    fun seek(ms: Long) { if (!onSilence()) { endedSent = false; exo.seekTo(0, ms) } }

    fun setVolume(v: Float) { exo.volume = v.coerceIn(0f, 1f) }

    /** Phones lose the network for a moment all the time: keep trying for about a minute; a missing file is final. */
    private class NetworkRetries : DefaultLoadErrorHandlingPolicy() {
        override fun getMinimumLoadableRetryCount(dataType: Int) = 20
        override fun getRetryDelayMsFor(info: LoadErrorHandlingPolicy.LoadErrorInfo): Long {
            val e = info.exception
            if (e is HttpDataSource.InvalidResponseCodeException && e.responseCode in 400..499) return C.TIME_UNSET
            return 3000
        }
    }

    companion object {
        private const val SILENCE = "silence"
        private const val NEXT = "next"
    }
}

/** What the page's script calls (window.MHApp). */
@UnstableApi
class PageBridge(private val app: App) {
    private val audio = app.audio
    private val main = Handler(Looper.getMainLooper())
    private fun on(f: () -> Unit) { main.post(f) }
    @JavascriptInterface fun load(url: String, atMs: Long, title: String, artist: String, art: String) =
        on { audio.load(url, atMs, title, artist, art) }
    @JavascriptInterface fun setNext(url: String, title: String, artist: String, art: String) =
        on { audio.setNext(url, title, artist, art) }
    @JavascriptInterface fun play() = on { audio.play() }
    @JavascriptInterface fun pause() = on { audio.pause() }
    @JavascriptInterface fun seek(ms: Long) = on { audio.seek(ms) }
    @JavascriptInterface fun setVolume(v: Float) = on { audio.setVolume(v) }
    // Find music's − / + in the notification: -1 = not Find music, 0 = neither chosen, 1 = −, 3 = +.
    @JavascriptInterface fun setVotes(v: Int) = on { audio.setVotes(if (v < 0) null else v) }
    // Download on a playlist's menu.
    @JavascriptInterface fun offlineState(pid: Int): String = app.offline.state(pid).toString()
    @JavascriptInterface fun offlineSet(pid: Int, on: Boolean) = app.offline.set(pid, on)
}
