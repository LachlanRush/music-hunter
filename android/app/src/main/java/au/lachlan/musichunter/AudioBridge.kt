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
import androidx.media3.datasource.HttpDataSource
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
 */
@UnstableApi
class AudioBridge(private val app: App) {
    private val main = Handler(Looper.getMainLooper())
    val exo: ExoPlayer = ExoPlayer.Builder(app)
        .setAudioAttributes(
            AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(), true,
        )
        .setHandleAudioBecomingNoisy(true)
        .setWakeMode(C.WAKE_MODE_NETWORK)
        .setMediaSourceFactory(DefaultMediaSourceFactory(app).setLoadErrorHandlingPolicy(NetworkRetries()))
        .build()

    /** The page whose track is loaded; it hears the player's events. */
    var owner: WebView? = null
        private set
    private var metadata = MediaMetadata.EMPTY
    private var announced = false          // loadedmetadata sent for this track
    private var endedSent = false
    var pageVisible = true

    /** What a page that doesn't own the player has asked for, applied when it presses play. */
    private class Pending(var url: String? = null, var seekMs: Long = 0, var volume: Float? = null,
                          var metadata: MediaMetadata? = null)
    private val pending = HashMap<WebView, Pending>()

    /** Called on the main thread whenever what's playing or its state changes (the now playing bar). */
    var onChange: () -> Unit = {}

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

    private fun announce() {
        if (exo.duration != C.TIME_UNSET && !onSilence()) lastDuration = exo.duration / 1000.0
        if (!announced && exo.duration != C.TIME_UNSET) { announced = true; emit("loadedmetadata") }
    }

    /** Position updates; less often with the screen off, since nobody's watching the bar. */
    private fun tick() {
        if (exo.isPlaying && !onSilence()) emit("timeupdate")
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

    private fun items(url: String): List<MediaItem> {
        val uri = app.offline.localAudio(url)?.let { Uri.fromFile(it) } ?: Uri.parse(url)   // downloaded: from the phone
        val track = MediaItem.Builder().setMediaId("track").setUri(uri).setMediaMetadata(metadata).build()
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
        announced = false; endedSent = false
        exo.setMediaItems(items(url), 0, 0)
        exo.prepare()
        onChange()
    }

    fun play(web: WebView) {
        if (owner !== web && owner != null && hasTrack) takeOver(web)
        else if (owner == null) owner = web
        app.connectService()
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
        onChange()
    }

    fun pause(web: WebView) { if (owner === web) exo.pause() }

    fun stop(web: WebView) {
        pending.remove(web)
        if (owner !== web) return
        exo.pause(); exo.clearMediaItems(); announced = false; endedSent = false; owner = null
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
    @JavascriptInterface fun setMetadata(title: String, artist: String, album: String, art: String) =
        on { audio.setMetadata(web, title, artist, album, art) }
    // The now playing bar (assets/native-audio.js), on a page the music doesn't come from.
    @JavascriptInterface fun npReady() = on { app.pushNowPlaying() }
    @JavascriptInterface fun npToggle() = on { audio.togglePlay() }
    @JavascriptInterface fun npAction(name: String) = on { if (name == "nexttrack" || name == "previoustrack") audio.action(name) }
    @JavascriptInterface fun npSeek(ms: Long) = on { audio.seekFromBar(ms) }
    @JavascriptInterface fun npShuffle() = on { app.toggleOwnerShuffle() }
    @JavascriptInterface fun npOpen() = on { app.showPlaying() }
    @JavascriptInterface fun pageReady() = on { app.pageReady(web) }
    // The Download switch on a playlist page.
    @JavascriptInterface fun offlineState(pid: Int): String = app.offline.state(pid).toString()
    @JavascriptInterface fun offlineSet(pid: Int, on: Boolean) = app.offline.set(pid, on)
}
