package au.lachlan.musichunter

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.WebView
import android.widget.FrameLayout
import androidx.media3.common.util.UnstableApi

@UnstableApi
class MainActivity : Activity() {
    private val app get() = App.of(this)
    private lateinit var pages: FrameLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (BuildConfig.DEBUG) intent.getStringExtra("server")?.let { app.server = it }
        pages = FrameLayout(this).apply { setBackgroundColor(Color.parseColor("#161618")) }
        setContentView(pages)
        app.attach(this)
        show(app.page())
        if (intent.getBooleanExtra(SHOW_PLAYING, false)) app.showPlaying()
        Updater.check(this)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.getBooleanExtra(SHOW_PLAYING, false)) app.showPlaying()   // the notification was tapped
    }

    private fun fill() = FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)

    /** Put a page on screen straight away (the first page). */
    fun show(web: WebView) {
        (web.parent as? ViewGroup)?.removeView(web)
        pages.removeAllViews()
        pages.addView(web, fill())
    }

    /** Start drawing a page behind the one on screen. */
    fun stage(web: WebView) {
        if (web.parent === pages) return
        (web.parent as? ViewGroup)?.removeView(web)
        pages.addView(web, 0, fill())
    }

    /** The playing page's full view, laid over the page on screen (#62). */
    fun overlay(web: WebView) {
        (web.parent as? ViewGroup)?.removeView(web)
        pages.addView(web, fill())
    }

    fun removeOverlay(web: WebView) { if (web.parent === pages) pages.removeView(web) }

    /** Swap `web` in once it has drawn, then take the others away (`done`). */
    fun reveal(web: WebView, done: () -> Unit) {
        stage(web)
        var finished = false
        val finish = {
            if (!finished) {
                finished = true
                android.util.Log.i("MusicHunter", "page swapped in")
                web.bringToFront()
                for (i in pages.childCount - 1 downTo 0) pages.getChildAt(i).takeIf { it !== web }?.let { pages.removeView(it) }
                done()
            }
        }
        if (!hasWindowFocus() && !isResumed()) { finish(); return }
        web.postVisualStateCallback(++visualRequest, object : WebView.VisualStateCallback() {
            override fun onComplete(requestId: Long) { web.post { finish() } }
        })
        web.postDelayed({ finish() }, 600)                // a page that never reports a frame
    }

    private var visualRequest = 0L
    private var resumed = false
    private fun isResumed() = resumed

    private var picking: android.webkit.ValueCallback<Array<android.net.Uri>>? = null

    fun chooseFile(callback: android.webkit.ValueCallback<Array<android.net.Uri>>, params: android.webkit.WebChromeClient.FileChooserParams) {
        picking?.onReceiveValue(null)
        picking = callback
        startActivityForResult(params.createIntent(), PICK)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == PICK) {
            picking?.onReceiveValue(android.webkit.WebChromeClient.FileChooserParams.parseResult(resultCode, data))
            picking = null
        }
    }

    override fun onStart() { super.onStart(); app.audio.pageVisible = true; app.pushNowPlaying() }
    override fun onResume() { super.onResume(); resumed = true; Updater.resume(this) }
    override fun onPause() { resumed = false; super.onPause() }
    override fun onStop() { app.audio.pageVisible = false; super.onStop() }

    @Deprecated("Back goes up a level, then leaves the app playing.")
    override fun onBackPressed() {
        if (!app.back()) moveTaskToBack(true)
    }

    override fun onDestroy() {
        pages.removeAllViews()
        app.detach(this)
        super.onDestroy()
    }

    companion object {
        const val SHOW_PLAYING = "show_playing"
        private const val PICK = 1
    }
}
