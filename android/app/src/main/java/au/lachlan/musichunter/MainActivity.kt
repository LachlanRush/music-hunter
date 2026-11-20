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

    /** Put the page on screen. */
    fun show(web: WebView) {
        (web.parent as? ViewGroup)?.removeView(web)
        pages.removeAllViews()
        pages.addView(web, fill())
    }

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

    override fun onStart() { super.onStart(); app.audio.pageVisible = true }
    override fun onResume() { super.onResume(); Updater.resume(this) }
    override fun onStop() { app.audio.pageVisible = false; super.onStop() }

    @Deprecated("Back goes up a level, then leaves the app playing.")
    override fun onBackPressed() {
        app.back { handled -> if (!handled) moveTaskToBack(true) }
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
