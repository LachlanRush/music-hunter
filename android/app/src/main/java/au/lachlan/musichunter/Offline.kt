package au.lachlan.musichunter

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.SystemClock
import android.util.Log
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import kotlin.concurrent.thread

/**
 * Offline on the phone (SPEC.md §19, step 2). A Download switch per playlist keeps its tracks, covers and track list
 * on the phone; the pages themselves are kept as they're visited. Everything the pages ask the Pi for goes through
 * here: from the Pi when it answers (and kept, if it's something offline needs), from what's kept when it doesn't.
 * Audio of downloaded tracks plays from the phone even when the Pi is there (quicker, and no data used).
 */
class Offline(private val context: Context, private val server: () -> String) {
    private val root = File(context.filesDir, "offline")
    private val audioDir = File(root, "audio")
    private val artDir = File(root, "art")
    private val webDir = File(root, "web")
    private val manifest = File(root, "playlists.json")
    private val lock = Any()
    @Volatile private var unreachableUntil = 0L
    @Volatile private var running = false
    @Volatile private var again = false
    private val progress = HashMap<Int, Pair<Int, Int>>()      // playlist -> (done, total) while downloading

    init { listOf(audioDir, artDir, webDir).forEach { it.mkdirs() } }

    // --- which playlists are downloaded --------------------------------------------------------------------------

    private fun playlists(): JSONObject = synchronized(lock) {
        if (manifest.exists()) JSONObject(manifest.readText()) else JSONObject()
    }

    private fun savePlaylists(j: JSONObject) = synchronized(lock) { manifest.writeText(j.toString()) }

    fun isOn(pid: Int) = playlists().optJSONObject(pid.toString())?.optBoolean("on") == true

    fun set(pid: Int, on: Boolean) {
        val j = playlists()
        if (on) j.put(pid.toString(), JSONObject().put("on", true)) else j.remove(pid.toString())
        savePlaylists(j)
        sync()
    }

    /** For the page's Download button. */
    fun state(pid: Int): JSONObject {
        val j = JSONObject().put("on", isOn(pid))
        val tracks = kept(pid)
        val p = synchronized(progress) { progress[pid] }
        j.put("tracks", tracks.size).put("stored", tracks.count { File(audioDir, it.toString()).exists() })
        if (p != null) j.put("done", p.first).put("total", p.second)
        j.put("bytes", tracks.sumOf { File(audioDir, it.toString()).length() })
        j.put("waitingForWifi", isOn(pid) && !unmetered())
        return j
    }

    private fun kept(pid: Int): List<Int> {
        val f = webFile("/api/playlists/$pid")
        if (!f.exists()) return emptyList()
        return try {
            val tracks = JSONObject(f.readText().substringAfter('\n')).getJSONArray("tracks")
            (0 until tracks.length()).map { tracks.getJSONObject(it) }.filter { !it.isNull("audio") }.map { it.getInt("id") }
        } catch (e: Exception) { emptyList() }
    }

    // --- audio for the player ------------------------------------------------------------------------------------

    /** A downloaded track's file for a page's audio address (`/audio/<id>`, also through Find music's address). */
    fun localAudio(url: String): File? {
        val id = Regex("/audio/(\\d+)$").find(url.substringBefore('?'))?.groupValues?.get(1) ?: return null
        if (!url.startsWith(server()) || url.contains("/find/")) return null      // Find music's ids are its own
        return File(audioDir, id).takeIf { it.exists() && it.length() > 0 }
    }

    // --- the pages' requests ---------------------------------------------------------------------------------------

    private fun keepable(path: String) = path == "/" || path.startsWith("/playlist/") || path.startsWith("/static/")
        || path == "/api/playlists" || Regex("^/api/playlists/\\d+$").matches(path)
        || path.startsWith("/art/") || path.startsWith("/cover/") || path == "/manifest.webmanifest"

    private fun webFile(key: String): File {
        val h = MessageDigest.getInstance("SHA-1").digest(key.toByteArray()).joinToString("") { "%02x".format(it) }
        return File(webDir, h)
    }

    /** Called by the pages' WebView for every request; null lets the WebView fetch it itself. */
    fun intercept(request: WebResourceRequest): WebResourceResponse? {
        if (request.method != "GET") return null
        val url = request.url.toString()
        val fonts = url.startsWith("https://fonts.googleapis.com/") || url.startsWith("https://fonts.gstatic.com/")
        if (!url.startsWith(server()) && !fonts) return null
        val bare = if (fonts) url else request.url.path.orEmpty().ifEmpty { "/" }
        if (!fonts && !keepable(bare)) {
            return if (SystemClock.elapsedRealtime() < unreachableUntil) unavailable() else null
        }
        val file = webFile(if (fonts) url else if (bare.startsWith("/cover/")) bare + "?" + request.url.query else bare)
        // Covers and fonts: what's kept shows at once, and is refreshed behind the scenes for next time (a page has dozens).
        // Pages and scripts come from the Pi (~10 ms each), or an old copy would hide an update, one page at a time; what's
        // kept is for when the Pi doesn't answer quickly.
        if ((fonts || bare.startsWith("/art/") || bare.startsWith("/cover/")) && file.exists()) {
            if (!fonts) refresh(url, file)
            return fromFile(file)
        }
        val quick = file.exists() && !bare.startsWith("/api/")
        if (SystemClock.elapsedRealtime() >= unreachableUntil) {
            try {
                val c = (URL(url).openConnection() as HttpURLConnection).apply {
                    connectTimeout = if (quick) 800 else 3000; readTimeout = if (quick) 2000 else 15000
                    request.requestHeaders.forEach { (k, v) -> if (k.lowercase() !in listOf("range", "accept-encoding")) setRequestProperty(k, v) }
                }
                val code = c.responseCode
                val type = c.contentType ?: "application/octet-stream"
                val body = (if (code < 400) c.inputStream else c.errorStream)?.use { it.readBytes() } ?: ByteArray(0)
                if (code == 200) store(file, type, body)
                return response(code, type, body)
            } catch (e: IOException) {
                unreachableUntil = SystemClock.elapsedRealtime() + 20_000       // don't wait on every request
                Log.i(TAG, "Pi unreachable (${e.message}); using what's kept")
            }
        }
        return if (file.exists()) fromFile(file) else unavailable()
    }

    private val refreshing = java.util.concurrent.Executors.newFixedThreadPool(2)
    private val refreshedAt = java.util.concurrent.ConcurrentHashMap<String, Long>()

    private fun refresh(url: String, file: File) {
        val now = SystemClock.elapsedRealtime()
        if (now < unreachableUntil || now - (refreshedAt[url] ?: -600_000L) < 300_000L) return   // at most every 5 min
        refreshedAt[url] = now
        refreshing.execute {
            try {
                val c = (URL(url).openConnection() as HttpURLConnection).apply { connectTimeout = 3000; readTimeout = 15000 }
                if (c.responseCode == 200) {
                    val type = c.contentType ?: "application/octet-stream"
                    store(file, type, c.inputStream.use { it.readBytes() })
                }
            } catch (e: IOException) { /* next time */ }
        }
    }

    private fun store(file: File, type: String, body: ByteArray) {
        val tmp = File(file.path + ".part")
        tmp.outputStream().use { it.write("$type\n".toByteArray()); it.write(body) }
        tmp.renameTo(file)
    }

    private fun fromFile(file: File): WebResourceResponse {
        val bytes = file.readBytes()
        val nl = bytes.indexOf('\n'.code.toByte())
        return response(200, String(bytes, 0, nl), bytes.copyOfRange(nl + 1, bytes.size))
    }

    private fun response(code: Int, contentType: String, body: ByteArray): WebResourceResponse {
        val mime = contentType.substringBefore(';').trim()
        val charset = Regex("charset=([^;]+)").find(contentType)?.groupValues?.get(1)?.trim() ?: "utf-8"
        val reason = if (code == 200) "OK" else "Error"
        // Fonts come from another site, so the page only uses them if they say they may be shared.
        val headers = mapOf("Cache-Control" to "no-store", "Access-Control-Allow-Origin" to "*")
        return WebResourceResponse(mime, charset, code, reason, headers, ByteArrayInputStream(body))
    }

    private fun unavailable() = WebResourceResponse("application/json", "utf-8", 503, "Offline",
        mapOf("Cache-Control" to "no-store"), ByteArrayInputStream("{\"detail\":\"Offline\"}".toByteArray()))

    // --- downloading -----------------------------------------------------------------------------------------------

    private fun unmetered(): Boolean {
        val cm = context.getSystemService(ConnectivityManager::class.java)
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
    }

    /** Bring the downloaded playlists up to date (on Wi-Fi only), and remove what's no longer wanted. */
    fun sync() {
        synchronized(lock) { if (running) { again = true; return }; running = true }
        thread(isDaemon = true, name = "offline-sync") {
            try {
                do {
                    again = false
                    runSync()
                } while (again)
            } finally {
                synchronized(lock) { running = false }
            }
        }
    }

    private fun get(path: String): Pair<String, ByteArray>? {
        val c = (URL(server() + path).openConnection() as HttpURLConnection).apply { connectTimeout = 5000; readTimeout = 60000 }
        if (c.responseCode != 200) return null
        return (c.contentType ?: "application/octet-stream") to c.inputStream.use { it.readBytes() }
    }

    private fun download(path: String, dest: File): Boolean {
        val c = (URL(server() + path).openConnection() as HttpURLConnection).apply { connectTimeout = 5000; readTimeout = 60000 }
        if (c.responseCode != 200) return false
        val tmp = File(dest.path + ".part")
        c.inputStream.use { input -> tmp.outputStream().use { input.copyTo(it, 1 shl 16) } }
        return tmp.renameTo(dest)
    }

    private fun runSync() {
        val on = playlists().keys().asSequence().mapNotNull { it.toIntOrNull() }.filter { isOn(it) }.toList()
        val wanted = HashSet<Int>()
        for (pid in on) wanted.addAll(kept(pid))
        if (on.isNotEmpty() && unmetered()) {
            try {
                cacheShell(on)
                for (pid in on) {
                    val (type, body) = get("/api/playlists/$pid") ?: continue
                    store(webFile("/api/playlists/$pid"), type, body)
                    val tracks = JSONObject(String(body)).getJSONArray("tracks")
                    val ids = (0 until tracks.length()).map { tracks.getJSONObject(it) }.filter { !it.isNull("audio") }
                    ids.forEach { wanted.add(it.getInt("id")) }
                    JSONObject(String(body)).optString("image").takeIf { it.isNotEmpty() && it != "null" }?.let { cache(it) }
                    var done = 0
                    synchronized(progress) { progress[pid] = 0 to ids.size }
                    for (t in ids) {
                        if (!isOn(pid)) break
                        val id = t.getInt("id")
                        val audio = File(audioDir, id.toString())
                        val quality = t.optInt("quality", 0)         // better files replace the old on the Pi (§19)
                        val mark = File(audioDir, "$id.q")
                        if (audio.exists() && mark.takeIf { it.exists() }?.readText()?.toIntOrNull() ?: 0 < quality) audio.delete()
                        if (!audio.exists() && download("/audio/$id", audio)) mark.writeText(quality.toString())
                        if (!t.isNull("art")) cache(t.getString("art"))
                        done++
                        synchronized(progress) { progress[pid] = done to ids.size }
                    }
                    synchronized(progress) { progress.remove(pid) }
                }
            } catch (e: IOException) {
                Log.i(TAG, "Offline sync stopped: ${e.message}")
                synchronized(progress) { progress.clear() }
            }
        }
        // Tracks no downloaded playlist has any more (a track removed, or a playlist's Download switched off).
        audioDir.listFiles()?.forEach { f ->
            val id = f.name.substringBefore('.').toIntOrNull()
            if (id == null || id !in wanted) f.delete()
        }
    }

    /** The pages themselves, so they open without the Pi: home, the playlists, their scripts, styles and fonts. */
    private fun cacheShell(playlists: List<Int>) {
        val pages = listOf("/", "/api/playlists") + playlists.map { "/playlist/$it" }
        val assets = HashSet<String>()
        for (p in pages) {
            val (type, body) = get(p) ?: continue
            store(webFile(p), type, body)
            if (type.startsWith("text/html")) {
                Regex("(?:src|href)=\"(/static/[^\"]+|https://fonts\\.googleapis\\.com/[^\"]+)\"").findAll(String(body))
                    .forEach { assets.add(it.groupValues[1].replace("&amp;", "&")) }
            }
        }
        for (a in assets) {
            if (a.startsWith("https://")) {
                val css = fetchExternal(a) ?: continue
                Regex("url\\((https://fonts\\.gstatic\\.com/[^)]+)\\)").findAll(String(css.second))
                    .forEach { m -> if (!webFile(m.groupValues[1]).exists()) fetchExternal(m.groupValues[1]) }
            } else cache(a)
        }
    }

    private fun cache(path: String) {
        val (type, body) = get(path) ?: return
        store(webFile(path), type, body)
    }

    private fun fetchExternal(url: String): Pair<String, ByteArray>? {
        val c = (URL(url).openConnection() as HttpURLConnection).apply {
            connectTimeout = 5000; readTimeout = 30000
            setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36")
        }
        if (c.responseCode != 200) return null
        val r = (c.contentType ?: "application/octet-stream") to c.inputStream.use { it.readBytes() }
        store(webFile(url), r.first, r.second)
        return r
    }

    companion object {
        private const val TAG = "MusicHunter"
    }
}
