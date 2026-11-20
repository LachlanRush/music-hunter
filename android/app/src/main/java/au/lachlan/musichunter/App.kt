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
import com.google.common.util.concurrent.ListenableFuture

/**
 * A page counted as hidden is frozen after about 5 minutes (its network requests and timers stop), and the page is
 * what asks the Pi for the next song. So it never hears that the window went away (found by a 25-minute screen-off test).
 */
private class PageView(context: Context) : WebView(context) {
    override fun onWindowVisibilityChanged(visibility: Int) {
        if (visibility != GONE) super.onWindowVisibilityChanged(VISIBLE)
    }
}

/**
 * Lives as long as the process. The page and the player belong here, not to the activity, so Android closing the
 * activity in the background doesn't take the page (which picks the next song) with it.
 *
 * One page for everything (SPEC.md §20): the one-page app moves between its screens itself and keeps one player, so
 * the app no longer keeps several pages alive or draws a player bar into them.
 */
@UnstableApi
class App : Application() {
    val audio: AudioBridge by lazy { AudioBridge(this) }
    val offline: Offline by lazy { Offline(this) { server } }
    var server: String = BuildConfig.SERVER
    private val webContext: MutableContextWrapper by lazy { MutableContextWrapper(this) }
    private var activity: MainActivity? = null
    private var controller: ListenableFuture<MediaController>? = null
    var updateOffered = false
    private var web: WebView? = null

    /** Dialogs and the picture picker need an activity while there is one. */
    fun attach(a: MainActivity) { activity = a; webContext.baseContext = a }
    fun detach(a: MainActivity) {
        if (activity === a) { activity = null; webContext.baseContext = this }
    }

    /** The page. It stays out of sight for a moment, until it has drawn its first screen. */
    fun page(): WebView = web ?: createWeb("$server${BuildConfig.START_PATH}").also {
        web = it
        audio.web = it
        it.alpha = 0f
        main.postDelayed({ it.animate().alpha(1f).setDuration(120).start() }, 400)
    }

    /** Connecting starts the service, which keeps playback alive in the background. */
    fun connectService() {
        if (controller != null) return
        controller = MediaController.Builder(this, SessionToken(this, ComponentName(this, PlayerService::class.java)))
            .buildAsync()
    }

    private val main = android.os.Handler(android.os.Looper.getMainLooper())

    override fun onCreate() {
        super.onCreate()
        syncOffline()
    }

    /** Downloaded playlists follow changes on the Pi: at launch and every half hour (on Wi-Fi only). */
    private fun syncOffline() {
        offline.sync()
        main.postDelayed({ syncOffline() }, 30 * 60_000L)
    }

    /** The notification was tapped: the playing song's full view. */
    fun showPlaying() { web?.evaluateJavascript("window.__mhShowPlaying&&__mhShowPlaying()", null) }

    /** Back goes up a level (the page decides); at the top the app goes to the background. */
    fun back(done: (Boolean) -> Unit) {
        val w = web ?: return done(false)
        w.evaluateJavascript("window.__mhBack?__mhBack():(history.length>1&&history.back(),false)") { done(it == "true") }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun createWeb(url: String): WebView {
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        val web = PageView(webContext)
        web.setBackgroundColor(PAGE_BG)   // never a white flash
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            userAgentString = "$userAgentString MusicHunterApp/${BuildConfig.VERSION_NAME}"
        }
        // The page picks the next song: Android shouldn't drop it first when the screen is off.
        web.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false)
        web.addJavascriptInterface(PageBridge(this), "MHApp")
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                val a = activity ?: return false             // choosing a playlist's picture
                a.chooseFile(callback, params)
                return true
            }
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest) = offline.intercept(request)
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                if (request.url.toString().startsWith(server)) return false
                startActivity(Intent(Intent.ACTION_VIEW, request.url).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                return true
            }
            override fun onPageFinished(view: WebView, url: String) {
                if (view.alpha < 1f) view.animate().alpha(1f).setDuration(120).start()
            }
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) view.loadDataWithBaseURL(server, offlinePage(request.url.toString()), "text/html", "utf-8", null)
            }
            override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
                Log.e(TAG, "Page process gone (crashed: ${detail.didCrash()})")
                val last = view.url?.takeIf { it.startsWith(server) } ?: "$server${BuildConfig.START_PATH}"
                audio.exo.pause()                          // the page that knew what comes next has gone
                (view.parent as? ViewGroup)?.removeView(view)
                view.destroy()
                this@App.web = null
                createWeb(last).also { this@App.web = it; audio.web = it; activity?.show(it) }
                return true
            }
        }
        web.loadUrl(url)
        return web
    }

    private fun offlinePage(retry: String) = """
        <!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
        <body style="background:#161618;color:#e8eaed;font:16px sans-serif;padding:32px">
        <h2>Can't reach the Pi</h2><p>Check that Tailscale is on and you have a connection.</p>
        <button style="padding:10px 18px;font-size:16px" onclick="location.href='$retry'">Try again</button>
    """.trimIndent()

    companion object {
        private const val TAG = "MusicHunter"
        val PAGE_BG = android.graphics.Color.parseColor("#161618")
        fun of(context: Context) = context.applicationContext as App
    }
}
