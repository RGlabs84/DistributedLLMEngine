package io.dlne.chat.update

import androidx.compose.runtime.Composable

/*
 * The Google Play build has no updater: Play updates the app, and an app may not install
 * updates of itself from anywhere else. These three empty functions are the only trace of the
 * feature here. They let the shared screens (DlneApp, ChatScreen, SettingsScreen) call the same
 * hooks in both flavors; the real ones are in src/sideload. This build has no
 * REQUEST_INSTALL_PACKAGES permission, no manifest address, no download, no installer code
 * (FlavorManifestTest checks the permission on every unit-test run).
 */

/** Sideload: checks for updates at start and hands the installer prompt to the screen. Play: nothing. */
@Composable
fun UpdateHost() = Unit

/** Sideload: the "update available" card at the top of the chat. Play: nothing. */
@Composable
fun UpdateBanner() = Unit

/** Sideload: the Updates section of Settings. Play: nothing. */
@Composable
fun UpdateSettingsSection() = Unit
