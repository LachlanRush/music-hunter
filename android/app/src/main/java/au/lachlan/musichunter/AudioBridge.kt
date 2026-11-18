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
 * The native player, driven by the pages (assets/native-audio.js). One player for the app, and one page owns it at
 * a time: the page that last pressed play. Another page (opened while music plays, see App) can prepare a track
 * without touching the music; pressing play hands the player over to it, and the page that had it is closed.
 *
 * Each track is queued with a stretch of silence behind it: when a track ends the player moves on to the silence and
 * stays "playing" while the page asks the Pi what comes next. A player that had stopped could lose its place in the
 * foreground, and Android doesn't let an app in the background get it back.
 *
 * The flow (2026-11-18): the page tells the player what comes next (`setNext`), and the player goes straight on to it
 * when a track ends, without waiting for the page — with the screen off the page can be slow to hear about it (#72:
 * a minute's silence until the app was opened). The page then catches up and confirms it (`load` with the same
 * address keeps it playing). Tracks the page expects to play soon are fetched ahead into a cache on the phone, so a
 * skip starts at once instead of waiting for the Pi.
 */
@UnstableApi
class AudioBridge(private val app: App) {
    private val main = Handler(Looper.getMainLooper())

    /** Tracks fetched ahead (and everything streamed), kept on the phone; emptied at each start, since a track's
     *  file on the Pi can change under the same address (a better copy, an extended mix). */
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

    /** The page whose track is loaded; it hears the player's events. */
    var owner: WebView? = null
        private set
    private var metadata = MediaMetadata.EMPTY
    private var announced = false          // loadedmetadata sent for this track
    private var endedSent = false
    private var nextUrl: String? = null            // what the page says comes next (queued behind the track)
    private var nextMeta: MediaMetadata = MediaMetadata.EMPTY
    private var advancedUrl: String? = null        // went on to it by itself; the page hasn't confirmed it yet
    var pageVisible = true

    /** What a page that doesn't own the player has asked for, applied when it presses play. */
    private class Pending(var url: String? = null, var seekMs: Long = 0, var volume: Float? = null,
                          var metadata: MediaMetadata? = null)
    private val pending = HashMap<WebView, Pending>()

    /** Called on the main thread whenever what's playing or its state changes (the now playing bar). */
    var onChange: () -> Unit = {}

    /** Find music's − / + for the notification (2026-11-11): null when the music isn't Find music, else 0 (neither
     *  chosen), 1 (−) or 3 (+). Only the page that has the music sets it; it clears when another page takes over. */
    var votes: Int? = null
        private set
    var votesWeb: WebView? = null
        private set
    var onVotes: () -> Unit = {}
    fun setVotes(web: WebView, v: Int?) {
        if (v != null && owner != null && owner !== web) return
        if (v == null && votesWeb != null && votesWeb !== web) return
        val changed = votes != v
        votes = v; votesWeb = if (v == null) null else web
        onVotes()
        if (changed) onChange()                              // the now playing bar on other pages shows them too
    }
    /** A − or + pressed in the notification: the page that has the music rates the song, as its own buttons do. */
    fun vote(dir: String) = js("window.__playerControl&&__playerControl('$dir')", votesWeb ?: owner)

    val hasTrack get() = exo.mediaItemCount > 0
    val title: String get() = metadata.title?.toString() ?: ""
    val artist: String get() = metadata.artist?.toString() ?: ""
    val art: String get() = metadata.artworkUri?.toString() ?: ""
    val positionS: Double get() = if (onSilence()) durationS else exo.currentPosition / 1000.0
    val durationS: Double get() = if (exo.duration == C.TIME_UNSET || onSilence()) lastDuration else exo.duration / 1000.0
    private var lastDuration = 0.0

    /** What the notification and lock screen control: next and previous go to the page, like the media keys. */
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
                onChange()
                if (onSilence()) return
                if (isPlaying) { announce(); emit("play"); emit("playing") }
                else if (!exo.playWhenReady) emit("pause")
            }
            override fun onMediaItemTransition(item: MediaItem?, reason: Int) {
                if (item?.mediaId == SILENCE && !endedSent) { endedSent = true; emit("ended") }
                if (item?.mediaId == NEXT) advance()
            }
            override fun onPositionDiscontinuity(old: Player.PositionInfo, new: Player.PositionInfo, reason: Int) {
                if (reason == Player.DISCONTINUITY_REASON_SEEK && !onSilence()) emit("seeked")
            }
            override fun onPlayerError(error: PlaybackException) {
                val s = JSONObject().put("code", 4).put("message", error.errorCodeName)
                js("window.__nativeAudio&&__nativeAudio('error',$s)")
            }
        })
        tick()
    }

    private fun onSilence() = exo.currentMediaItem?.mediaId == SILENCE

    /** The track ended and the player went on to the next one by itself: it's now the track; the page is told
     *  (it ends the old one and confirms this one when it can). */
    private fun advance() {
        val url = nextUrl ?: return
        advancedUrl = url; nextUrl = null
        metadata = nextMeta
        main.post {
            if (exo.currentMediaItemIndex > 0) exo.removeMediaItems(0, exo.currentMediaItemIndex)
            val last = exo.mediaItemCount - 1
            if (last >= 0 && exo.getMediaItemAt(last).mediaId == SILENCE)
                exo.replaceMediaItem(last, exo.getMediaItemAt(last).buildUpon().setMediaMetadata(metadata).build())
        }
        announced = false; endedSent = false
        js("window.__nativeAudio&&__nativeAudio('advanced',${JSONObject().put("url", url)})")
        onChange()
    }

    /** Fetch a track into the phone's cache now (the next one, or Find music's pick most likely next). */
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
        if (exo.duration != C.TIME_UNSET && !onSilence()) lastDuration = exo.duration / 1000.0
        if (!announced && exo.duration != C.TIME_UNSET) { announced = true; emit("loadedmetadata") }
    }

    /** Position updates; less often with the screen off, since nobody's watching the bar. */
    private fun tick() {
        if (exo.isPlaying && !onSilence() && advancedUrl == null) emit("timeupdate")
        main.postDelayed({ tick() }, if (pageVisible) 250 else 1000)
    }

    private fun state(): JSONObject = JSONObject().apply {
        if (!onSilence()) {
            put("t", exo.currentPosition / 1000.0)
            if (exo.duration != C.TIME_UNSET) put("d", exo.duration / 1000.0)
        }
    }

    private fun emit(type: String) = js("window.__nativeAudio&&__nativeAudio('$type',${state()})")
    fun action(name: String) = js("window.__nativeAction&&__nativeAction('$name')")
    private fun js(code: String, to: WebView? = null) { main.post { (to ?: owner)?.evaluateJavascript(code, null) } }

    private fun uriFor(url: String): Uri =
        app.offline.localAudio(url)?.let { Uri.fromFile(it) } ?: Uri.parse(url)              // downloaded: from the phone

    private fun items(url: String): List<MediaItem> {
        nextUrl = null; advancedUrl = null
        val track = MediaItem.Builder().setMediaId("track").setUri(uriFor(url)).setMediaMetadata(metadata).build()
        val silence = MediaItem.Builder().setMediaId(SILENCE).setUri("asset:///silence.ogg").setMediaMetadata(metadata).build()
        return listOf(track, silence)
    }

    /** Whether `web` may drive the player now: it owns it, or nothing is loaded, so taking it disturbs nothing. */
    private fun mayDrive(web: WebView): Boolean {
        if (owner === web) return true
        if (owner == null || !hasTrack) { owner = web; pending.remove(web); return true }
        return false
    }

    // --- what the pages call (through PageBridge, on the main thread) -------------------------------------------

    fun load(web: WebView, url: String) {
        if (!mayDrive(web)) { pending.getOrPut(web) { Pending() }.apply { this.url = url; seekMs = 0 }; return }
        if (advancedUrl != null && url == advancedUrl) {        // the page confirms what's already playing
            advancedUrl = null
            announce()
            if (exo.isPlaying) { emit("play"); emit("playing") }
            onChange()
            return
        }
        announced = false; endedSent = false
        exo.setMediaItems(items(url), 0, 0)
        exo.prepare()
        onChange()
    }

    /** What comes after the track playing ("" for nothing): queued behind it, and fetched into the cache. */
    fun setNext(web: WebView, url: String, title: String, artist: String, art: String) {
        if (owner !== web || !hasTrack) return
        val meta = MediaMetadata.Builder().setTitle(title).setArtist(artist)
            .setArtworkUri(if (art.isEmpty()) null else Uri.parse(art)).build()
        // (only after the track playing: a track the player went on to by itself keeps its "next" id)
        val i = (exo.currentMediaItemIndex + 1 until exo.mediaItemCount).firstOrNull { exo.getMediaItemAt(it).mediaId == NEXT }
        if (url.isEmpty()) { if (i != null) exo.removeMediaItem(i); nextUrl = null; return }
        if (onSilence() || advancedUrl != null) return          // too late, or the page hasn't caught up yet
        if (nextUrl == url && i != null) { nextMeta = meta; exo.replaceMediaItem(i, exo.getMediaItemAt(i).buildUpon().setMediaMetadata(meta).build()); return }
        nextUrl = url; nextMeta = meta
        val item = MediaItem.Builder().setMediaId(NEXT).setUri(uriFor(url)).setMediaMetadata(meta).build()
        if (i != null) exo.replaceMediaItem(i, item) else exo.addMediaItem(exo.currentMediaItemIndex + 1, item)
        preload(url)
    }

    fun play(web: WebView) {
        if (owner !== web && owner != null && hasTrack) takeOver(web)
        else if (owner == null) owner = web
        app.connectService()
        app.rememberPlaying(web.url)
        if (onSilence()) { endedSent = false; exo.seekTo(0, 0) }   // played again after it ended
        exo.play()
    }

    /** `web` pressed play while another page had the music: it gets the player, and that page is closed. */
    private fun takeOver(web: WebView) {
        val old = owner
        val p = pending.remove(web) ?: Pending()
        owner = web
        p.metadata?.let { applyMetadata(it) }
        p.volume?.let { exo.volume = it }
        p.url?.let { url ->
            announced = false; endedSent = false
            exo.setMediaItems(items(url), 0, p.seekMs)
            exo.prepare()
        }
        if (old != null) app.pageLostMusic(old)
        if (votesWeb != null && votesWeb !== web) { votes = null; votesWeb = null; onVotes() }
        onChange()
    }

    fun pause(web: WebView) { if (owner === web) exo.pause() }

    fun stop(web: WebView) {
        pending.remove(web)
        if (owner !== web) return
        exo.pause(); exo.clearMediaItems(); announced = false; endedSent = false; owner = null
        nextUrl = null; advancedUrl = null
        onChange()
    }

    fun seek(web: WebView, ms: Long) {
        if (!mayDrive(web)) { pending.getOrPut(web) { Pending() }.seekMs = ms; return }
        endedSent = false
        exo.seekTo(0, ms)
    }

    fun setVolume(web: WebView, v: Float) {
        val x = v.coerceIn(0f, 1f)
        if (owner === web || owner == null) exo.volume = x else pending.getOrPut(web) { Pending() }.volume = x
    }

    fun setMetadata(web: WebView, title: String, artist: String, album: String, art: String) {
        val m = MediaMetadata.Builder().setTitle(title).setArtist(artist).setAlbumTitle(album)
            .setArtworkUri(if (art.isEmpty()) null else Uri.parse(art)).build()
        if (owner === web || owner == null) applyMetadata(m) else pending.getOrPut(web) { Pending() }.metadata = m
    }

    private fun applyMetadata(m: MediaMetadata) {
        metadata = m
        for (i in 0 until exo.mediaItemCount) {
            if (i > exo.currentMediaItemIndex && exo.getMediaItemAt(i).mediaId == NEXT) continue   // has its own
            exo.replaceMediaItem(i, exo.getMediaItemAt(i).buildUpon().setMediaMetadata(m).build())
        }
        onChange()
    }

    /** A page went away (closed, or its process died). */
    fun forget(web: WebView) {
        pending.remove(web)
        if (owner === web) { exo.pause(); exo.clearMediaItems(); owner = null; announced = false; endedSent = false; onChange() }
    }

    // --- the now playing bar on other pages: it drives the page the music comes from -----------------------------

    fun togglePlay() {
        if (exo.isPlaying) exo.pause()
        else { app.connectService(); if (onSilence()) { endedSent = false; exo.seekTo(0, 0) }; exo.play() }
    }
    fun seekFromBar(ms: Long) { if (!onSilence()) { endedSent = false; exo.seekTo(0, ms) } }

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

/** One per page: what the page's script calls, tagged with which page it is. */
@UnstableApi
class PageBridge(private val app: App, private val web: WebView) {
    private val audio = app.audio
    private val main = Handler(Looper.getMainLooper())
    private fun on(f: () -> Unit) { main.post(f) }
    @JavascriptInterface fun load(url: String) = on { audio.load(web, url) }
    @JavascriptInterface fun play() = on { audio.play(web) }
    @JavascriptInterface fun pause() = on { audio.pause(web) }
    @JavascriptInterface fun stop() = on { audio.stop(web) }
    @JavascriptInterface fun seek(ms: Long) = on { audio.seek(web, ms) }
    @JavascriptInterface fun setVolume(v: Float) = on { audio.setVolume(web, v) }
    @JavascriptInterface fun setNext(url: String, title: String, artist: String, art: String) =
        on { audio.setNext(web, url, title, artist, art) }
    @JavascriptInterface fun preload(url: String) = on { audio.preload(url) }
    // Find music's − / + in the notification: -1 = not Find music, 0 = neither chosen, 1 = −, 3 = +.
    @JavascriptInterface fun setVotes(v: Int) = on { audio.setVotes(web, if (v < 0) null else v) }
    @JavascriptInterface fun setMetadata(title: String, artist: String, album: String, art: String) =
        on { audio.setMetadata(web, title, artist, album, art) }
    // The now playing bar (assets/native-audio.js), on a page the music doesn't come from.
    @JavascriptInterface fun npReady() = on { app.pushNowPlaying() }
    @JavascriptInterface fun npToggle() = on { audio.togglePlay() }
    @JavascriptInterface fun npAction(name: String) = on { if (name == "nexttrack" || name == "previoustrack") audio.action(name) }
    @JavascriptInterface fun npSeek(ms: Long) = on { audio.seekFromBar(ms) }
    @JavascriptInterface fun npShuffle() = on { app.toggleOwnerShuffle() }
    @JavascriptInterface fun npVote(dir: String) = on { if (dir == "up" || dir == "down") audio.vote(dir) }
    // The bar on another page: the playing song's full view over it (#62). From that view, "Playing from …": its page.
    @JavascriptInterface fun npOpen() = on { if (web === app.overlay) app.focusOverlay() else app.showPlayingView() }
    @JavascriptInterface fun npClosed() = on { app.overlayClosed(web) }
    @JavascriptInterface fun npView(open: Boolean) = on { app.viewOpen(web, open) }
    @JavascriptInterface fun pageReady() = on { app.pageReady(web) }
    // "Add to queue" on a page the music doesn't come from: the song joins the playing page's queue (as the browser
    // shell does). "" when nothing plays elsewhere (the page queues it itself), "unsupported" while Find music plays.
    @JavascriptInterface fun enqueue(trackJson: String): String {
        val result = java.util.concurrent.atomic.AtomicReference("")
        val done = java.util.concurrent.CountDownLatch(1)
        main.post {
            try {
                val owner = audio.owner
                if (owner != null && owner !== web && audio.hasTrack) {
                    if (owner.url?.contains("/find/") == true || audio.votes != null) result.set("unsupported")
                    else { owner.evaluateJavascript("window.__enqueue && __enqueue($trackJson)", null); result.set("queued") }
                }
            } finally { done.countDown() }
        }
        done.await(1, java.util.concurrent.TimeUnit.SECONDS)
        return result.get()
    }
    // The Download switch on a playlist page.
    @JavascriptInterface fun offlineState(pid: Int): String = app.offline.state(pid).toString()
    @JavascriptInterface fun offlineSet(pid: Int, on: Boolean) = app.offline.set(pid, on)
}
