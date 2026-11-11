package au.lachlan.musichunter

import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import androidx.core.content.FileProvider
import androidx.media3.common.util.UnstableApi
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import kotlin.concurrent.thread

/**
 * Updates (SPEC.md §19): the newest build is kept on the Pi (`music-hunter pi app` sends it there). At launch the app
 * asks the Pi for its version; if it's newer, it offers to update, downloads it, checks it, and hands it to Android,
 * which asks for one tap. Android only installs it if it's signed with the same key as the app already on the phone.
 */
@UnstableApi
object Updater {
    private const val TAG = "MusicHunter"
    private val main = Handler(Looper.getMainLooper())
    private var ready: File? = null              // downloaded, waiting for "install unknown apps" to be allowed

    fun check(activity: MainActivity) {
        val app = App.of(activity)
        if (BuildConfig.DEBUG || app.updateOffered) return     // the dev build isn't updated from the Pi
        app.updateOffered = true
        thread(isDaemon = true) {
            try {
                val latest = JSONObject(get("${app.server}/app/latest.json") ?: return@thread)
                val code = latest.getInt("version_code")
                if (code <= BuildConfig.VERSION_CODE) return@thread
                main.post { offer(activity, latest) }
            } catch (e: Exception) {
                Log.i(TAG, "No update check: ${e.message}")
            }
        }
    }

    private fun offer(activity: MainActivity, latest: JSONObject) {
        if (activity.isFinishing) return
        AlertDialog.Builder(activity)
            .setTitle("Update available")
            .setMessage("Version ${latest.getString("version_name")} is ready (you have ${BuildConfig.VERSION_NAME}).")
            .setPositiveButton("Update") { _, _ -> download(activity, latest) }
            .setNegativeButton("Later", null)
            .show()
    }

    private fun download(activity: MainActivity, latest: JSONObject) {
        val app = App.of(activity)
        val file = File(activity.cacheDir, "updates/music-hunter.apk").apply { parentFile?.mkdirs() }
        val progress = AlertDialog.Builder(activity).setMessage("Downloading the update…").setCancelable(false).show()
        thread(isDaemon = true) {
            val ok = try {
                val c = URL(app.server + latest.getString("url")).openConnection() as HttpURLConnection
                c.connectTimeout = 15000; c.readTimeout = 60000
                val digest = MessageDigest.getInstance("SHA-256")
                c.inputStream.use { input ->
                    file.outputStream().use { out ->
                        val buf = ByteArray(1 shl 16)
                        while (true) {
                            val n = input.read(buf); if (n < 0) break
                            digest.update(buf, 0, n); out.write(buf, 0, n)
                        }
                    }
                }
                digest.digest().joinToString("") { "%02x".format(it) } == latest.optString("sha256")
            } catch (e: Exception) {
                Log.e(TAG, "Update download failed", e); false
            }
            main.post {
                progress.dismiss()
                if (!ok) {
                    AlertDialog.Builder(activity).setMessage("The update didn't download properly. Try again at the next launch.")
                        .setPositiveButton("OK", null).show()
                    App.of(activity).updateOffered = false
                    return@post
                }
                ready = file
                install(activity)
            }
        }
    }

    /** Back from the settings page where "install unknown apps" was allowed. */
    fun resume(activity: MainActivity) { if (ready != null) install(activity) }

    private fun install(activity: MainActivity) {
        val file = ready ?: return
        if (!activity.packageManager.canRequestPackageInstalls()) {
            AlertDialog.Builder(activity)
                .setTitle("Allow updates")
                .setMessage("Android needs your permission, once, for Music Hunter to install its own updates.")
                .setPositiveButton("Open settings") { _, _ ->
                    activity.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${activity.packageName}")))
                }
                .setNegativeButton("Later") { _, _ -> ready = null }
                .show()
            return
        }
        ready = null
        val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.updates", file)
        activity.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    private fun get(url: String): String? {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = 8000; c.readTimeout = 8000
        return if (c.responseCode == 200) c.inputStream.bufferedReader().use { it.readText() } else null
    }
}
