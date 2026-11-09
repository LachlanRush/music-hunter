package au.lachlan.musichunter

import android.app.PendingIntent
import android.content.Intent
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService

/** Keeps playing in the background, as a foreground media service with the notification, like Spotify's player. */
@UnstableApi
class PlayerService : MediaSessionService() {
    private var session: MediaSession? = null

    override fun onCreate() {
        super.onCreate()
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(MainActivity.SHOW_PLAYING, true),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        session = MediaSession.Builder(this, App.of(this).audio.sessionPlayer).setSessionActivity(open).build()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo) = session

    /** Swiped away from the recent apps: like Spotify, the music stops and the app ends (next launch starts fresh). */
    override fun onTaskRemoved(rootIntent: Intent?) {
        App.of(this).audio.exo.run { pause(); clearMediaItems() }
        session?.release()
        session = null
        stopSelf()
        android.os.Process.killProcess(android.os.Process.myPid())
    }

    override fun onDestroy() {
        session?.release()         // the player itself belongs to the app
        session = null
        super.onDestroy()
    }
}
