package au.lachlan.musichunter

import android.app.PendingIntent
import android.content.Intent
import android.os.Bundle
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.CommandButton
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import androidx.media3.session.SessionCommand
import androidx.media3.session.SessionResult
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

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
        val audio = App.of(this).audio
        session = MediaSession.Builder(this, audio.sessionPlayer).setSessionActivity(open)
            .setCallback(object : MediaSession.Callback {
                override fun onConnect(s: MediaSession, c: MediaSession.ControllerInfo): MediaSession.ConnectionResult =
                    MediaSession.ConnectionResult.AcceptedResultBuilder(s).setAvailableSessionCommands(
                        MediaSession.ConnectionResult.DEFAULT_SESSION_COMMANDS.buildUpon().add(UP).add(DOWN).build(),
                    ).build()
                override fun onCustomCommand(s: MediaSession, c: MediaSession.ControllerInfo, command: SessionCommand,
                                             args: Bundle): ListenableFuture<SessionResult> {
                    when (command.customAction) { UP.customAction -> audio.vote("up"); DOWN.customAction -> audio.vote("down") }
                    return Futures.immediateFuture(SessionResult(SessionResult.RESULT_SUCCESS))
                }
            }).build()
        audio.onVotes = { showVotes() }
        showVotes()
    }

    /** Find music's − / + in the notification and on the lock screen (2026-11-11, like Spotify's +): filled once
     *  chosen; gone when the music isn't Find music. */
    private fun showVotes() {
        val v = App.of(this).audio.votes
        session?.setCustomLayout(if (v == null) emptyList() else listOf(
            CommandButton.Builder(if (v == 1) CommandButton.ICON_MINUS_CIRCLE_FILLED else CommandButton.ICON_MINUS_CIRCLE_UNFILLED)
                .setDisplayName("Not for this playlist").setSessionCommand(DOWN).build(),
            CommandButton.Builder(if (v == 3) CommandButton.ICON_PLUS_CIRCLE_FILLED else CommandButton.ICON_PLUS_CIRCLE_UNFILLED)
                .setDisplayName("Add to this playlist").setSessionCommand(UP).build(),
        ))
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
        App.of(this).audio.onVotes = {}
        session?.release()         // the player itself belongs to the app
        session = null
        super.onDestroy()
    }

    companion object {
        private val UP = SessionCommand("vote_up", Bundle.EMPTY)
        private val DOWN = SessionCommand("vote_down", Bundle.EMPTY)
    }
}
