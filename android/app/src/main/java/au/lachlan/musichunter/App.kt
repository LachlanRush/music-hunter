package au.lachlan.musichunter

import android.annotation.SuppressLint
import android.app.Application
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.MutableContextWrapper
import android.net.Uri
import android.util.Log
import android.view.ViewGroup
import android.webkit.RenderProcessGoneDetail
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.google.common.util.concurrent.ListenableFuture

/**
 * A page counted as hidden is frozen after about 5 minutes (its network requests and timers stop), and the page is
 * what fetches the next track. So it never hears that the window went away (found by a 25-minute screen-off test).
 */
private class PageView(context: Context) : WebView(context) {
    override fun onWindowVisibilityChanged(visibility: Int) {
        if (visibility != GONE) super.onWindowVisibilityChanged(VISIBLE)
    }
}

/**
 * Lives as long as the process. The pages and the player belong here, not to the activity, so Android closing the
 * activity in the background doesn't take the page (which picks the next track) with it.
 *
 * Music carries on between pages (SPEC.md §19): while a page has a track loaded, going to another page opens that
 * page alongside it instead of leaving, and a bar at the bottom shows what's playing. Going back to the playing page
 * (with back, the bar, or a link to it) shows it again; pressing play on the other page hands the music over.
 */
@UnstableApi
class App : Application() {
    val audio: AudioBridge by lazy { AudioBridge(this) }
    val offline: Offline by lazy { Offline(this) { server } }
    var server: String = BuildConfig.SERVER
    /** The page on screen, and (while music plays from another page) the playing page behind it. */
    var front: WebView? = null
        private set
    private val webContext: MutableContextWrapper by lazy { MutableContextWrapper(this) }
    private var activity: MainActivity? = null
    private var controller: ListenableFuture<MediaController>? = null
    var updateOffered = false

    /** Dialogs (confirm, prompt) need an activity while there is one. */
    fun attach(a: MainActivity) { activity = a; webContext.baseContext = a }
    fun detach(a: MainActivity) {
        if (activity === a) { activity = null; webContext.baseContext = this }
    }

    /** The page on screen. The first one stays out of sight until it has its content, as later pages do. */
    fun page(): WebView = front ?: createWeb("$server/").also {
        front = it
        it.alpha = 0f
        main.postDelayed({ it.alpha = 1f }, 800)
    }

    // --- moving between pages (SPEC.md §19): the next page is built behind the one on screen and swapped in once
    // it has drawn, so there's no blank page in between. -----------------

    private var incoming: WebView? = null            // being built behind the page on screen
    private val kept = LinkedHashMap<String, WebView>()   // pages visited, alive: going back to one is instant

    private fun key(url: String?) = url.orEmpty().substringBefore('#').trimEnd('/')

    private var openedAt = 0L
    private fun open(url: String) {
        openedAt = android.os.SystemClock.elapsedRealtime()
        val playing = audio.owner?.takeIf { audio.hasTrack && it !== front }
        if (playing != null && samePage(url, playing.url)) { reveal(playing); return }   // the playing page: as it is
        kept.remove(key(url))?.let { page ->                                             // seen lately: as it was, then updated
            incoming?.let { if (it !== front && it !== audio.owner) close(it) }
            incoming = null
            reveal(page)
            page.evaluateJavascript("typeof load==='function'&&load();typeof loadFind==='function'&&loadFind()", null)
            return
        }
        incoming?.let { if (it !== front && it !== audio.owner) close(it) }
        val web = createWeb(url)
        incoming = web
        activity?.stage(web)
        main.postDelayed({ if (incoming === web) reveal(web) }, 800)       // a slow answer: show the page, it fills in
    }

    /** The page's script says it has its content (assets/native-audio.js). */
    fun pageReady(web: WebView) {
        if (incoming === web) Log.i(TAG, "page ready after ${android.os.SystemClock.elapsedRealtime() - openedAt} ms")
        if (incoming === web) reveal(web)
        else if (web === front && web.alpha < 1f) web.animate().alpha(1f).setDuration(120).start()
    }

    private fun reveal(web: WebView) {
        if (incoming === web) incoming = null
        val was = front
        front = web
        val a = activity
        val swap = {
            Log.i(TAG, "page on screen ${android.os.SystemClock.elapsedRealtime() - openedAt} ms after the tap")
            if (was != null && was !== web) {
                (was.parent as? ViewGroup)?.removeView(was)
                if (!(was === audio.owner && audio.hasTrack)) keep(was)             // the playing page stays as it is
            }
            if (was != null && was !== web) was.evaluateJavascript("window.__nowPlaying&&__nowPlaying({show:false})", null)
        }
        if (a != null) a.reveal(web, swap) else swap()
        refreshOwnerShuffle()
        pushNowPlaying()
    }

    /** The playing page, if it isn't the one on screen. */
    val playingBehind: WebView? get() = audio.owner?.takeIf { it !== front && audio.hasTrack }

    /** Connecting starts the service, which keeps playback alive in the background. */
    fun connectService() {
        if (controller != null) return
        controller = MediaController.Builder(this, SessionToken(this, ComponentName(this, PlayerService::class.java)))
            .buildAsync()
    }


    // --- now playing on other pages (assets/native-audio.js draws it like a playlist page's player) ------------

    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private var ownerShuffle: Boolean? = null       // the playing page's shuffle, when it's a playlist page

    /** Tell the page on screen what's playing (it shows the bar only when the music comes from another page). */
    fun pushNowPlaying() {
        val web = front ?: return
        val show = playingBehind != null
        val s = org.json.JSONObject().put("show", show)
        if (show) {
            s.put("title", audio.title).put("artist", audio.artist).put("art", audio.art)
                .put("playing", audio.exo.playWhenReady).put("t", audio.positionS).put("d", audio.durationS)
                .put("shuffle", ownerShuffle ?: org.json.JSONObject.NULL)
        }
        web.evaluateJavascript("window.__nowPlaying&&__nowPlaying($s)", null)
    }

    private fun tickNowPlaying() {
        if (playingBehind != null && audio.pageVisible) pushNowPlaying()
        main.postDelayed({ tickNowPlaying() }, 500)
    }

    fun onPlayerChange() { refreshOwnerShuffle(); pushNowPlaying() }

    private fun refreshOwnerShuffle() {
        val owner = audio.owner ?: run { ownerShuffle = null; return }
        owner.evaluateJavascript("(()=>{const e=document.getElementById('shuffle');return e&&location.pathname.startsWith('/playlist/')?e.getAttribute('aria-pressed'):null})()") {
            val v = it?.trim('"')
            val next = when (v) { "true" -> true; "false" -> false; else -> null }
            if (next != ownerShuffle) { ownerShuffle = next; pushNowPlaying() }
        }
    }

    fun toggleOwnerShuffle() {
        audio.owner?.evaluateJavascript("(()=>{const e=document.getElementById('shuffle');if(e)e.click()})()") { refreshOwnerShuffle() }
    }

    override fun onCreate() {
        super.onCreate()
        audio.onChange = { onPlayerChange() }
        tickNowPlaying()
        syncOffline()
    }

    /** Downloaded playlists follow changes on the Pi: at launch and every half hour (on Wi-Fi only). */
    private fun syncOffline() {
        offline.sync()
        main.postDelayed({ syncOffline() }, 30 * 60_000L)
    }

    /** Back to the playing page (the bar, the notification). */
    fun showPlaying() { playingBehind?.let { reveal(it) } }

    /** Back goes up a level, not to wherever I was before: Find music and Results to their playlist, a playlist to
     *  the playlists. Returns false at the top (the app then goes to the background). */
    fun back(): Boolean {
        val path = Uri.parse(front?.url ?: return false).path.orEmpty().trimEnd('/')
        val up = Regex("^/find/(\\d+)").find(path)?.let { "/playlist/${it.groupValues[1]}" }
            ?: Regex("^/playlist/(\\d+)/.+").find(path)?.let { "/playlist/${it.groupValues[1]}" }
            ?: if (path.isEmpty()) return false else "/"
        open("$server$up")
        return true
    }

    /** Another page took the music from this one: like leaving a page in a browser, it's closed. */
    fun pageLostMusic(web: WebView) { if (web !== front) close(web) }

    /** Find music isn't kept: its page holds a play, which should only ever be the current one. */
    private fun keep(web: WebView) {
        val url = web.url
        if (url == null || Uri.parse(url).path.orEmpty().startsWith("/find/")) { close(web); return }
        kept.remove(key(url))?.let { if (it !== web) close(it) }
        kept[key(url)] = web
        while (kept.size > MAX_KEPT) kept.remove(kept.keys.first())?.let { close(it) }
    }

    private fun close(web: WebView) {
        kept.values.remove(web)
        audio.forget(web)
        (web.parent as? ViewGroup)?.removeView(web)
        web.destroy()
    }

    private fun samePage(a: String?, b: String?): Boolean {
        fun key(u: String?) = u?.substringBefore('#')?.trimEnd('/')
        return a != null && key(a) == key(b)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun createWeb(url: String): WebView {
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        val web = PageView(webContext)
        web.setBackgroundColor(android.graphics.Color.parseColor("#111417"))   // never a white flash
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            userAgentString = "$userAgentString MusicHunterApp/${BuildConfig.VERSION_NAME}"
        }
        // The page picks the next track: Android shouldn't drop it first when the screen is off.
        web.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false)
        web.addJavascriptInterface(PageBridge(this, web), "NativeAudioBridge")
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            val script = assets.open("native-audio.js").bufferedReader().use { it.readText() }
            WebViewCompat.addDocumentStartJavaScript(web, script, setOf(Uri.parse(server).let { "${it.scheme}://${it.authority}" }))
        } else {
            Log.e(TAG, "This WebView can't run scripts at page start; update Android System WebView")
        }
        web.webChromeClient = object : WebChromeClient() {   // confirm, prompt and alert dialogs by default
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                val a = activity ?: return false             // choosing a playlist's picture
                a.chooseFile(callback, params)
                return true
            }
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest) = offline.intercept(request)
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val target = request.url.toString()
                if (!target.startsWith(server)) {
                    startActivity(Intent(Intent.ACTION_VIEW, request.url).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    return true
                }
                if (!request.isForMainFrame || view !== front) return false
                open(target)                              // built behind, swapped in when drawn
                return true
            }
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) view.loadDataWithBaseURL(server, offlinePage(request.url.toString()), "text/html", "utf-8", null)
            }
            override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
                Log.e(TAG, "Page process gone (crashed: ${detail.didCrash()})")
                val last = view.url?.takeIf { it.startsWith(server) } ?: "$server/"
                val wasFront = view === front
                close(view)                               // its music stops: the page that knew what comes next has gone
                if (wasFront) createWeb(last).also { front = it; activity?.reveal(it) {} }
                return true
            }
        }
        web.loadUrl(url)
        return web
    }

    private fun offlinePage(retry: String) = """
        <!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
        <body style="background:#111417;color:#e8eaed;font:16px sans-serif;padding:32px">
        <h2>Can't reach the Pi</h2><p>Check that Tailscale is on and you have a connection.</p>
        <button style="padding:10px 18px;font-size:16px" onclick="location.href='$retry'">Try again</button>
    """.trimIndent()

    companion object {
        private const val TAG = "MusicHunter"
        private const val MAX_KEPT = 4
        fun of(context: Context) = context.applicationContext as App
    }
}
