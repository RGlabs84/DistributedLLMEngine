# DLNE Chat for Android: update log

What changed in each version of the app. The newest is at the top; "Unreleased" is built and tested but not on the
site yet (owner, 2026-11-02: fewer releases, a real update log).

## [1.1.1] - 2026-11-02

### Added
- **A proper maintenance screen.** While DLNE is being upgraded (the server says so in its status, or refuses a
  message with the maintenance answer) the app shows one full-screen card in DLNE's colours: the logo, "DLNE is being
  upgraded", the server's message and "Expected back". Sending is paused meanwhile and what you typed stays in the
  box. The app asks every 15 seconds and the card goes away by itself when the upgrade is over.

### Fixed
- **Holding your place while an answer streams.** With a finger on the chat (holding a spot, or scrolling up the
  moment the answer starts), the words under it now stay put while the answer grows; before, they only stayed once
  the scroll had ended.
- **No jump when an answer finishes.** Reading higher up when an answer ended, the text slid up a couple of lines as
  the answer got its copy and report buttons; it now stays where it is.
- **The keyboard closes when you open the menu** (the ☰ button or a swipe from the edge), so it no longer covers your
  chats.

## [1.1.0] - 2026-11-02

### Added
- **Conversation mode** (Settings > Voice mode): a spoken message sends itself and the answer is read aloud by the
  phone's own voice. Dictate (the default) keeps your words in the box to check first.

## [1.0.9] - 2026-11-02

### Fixed
- Scrolling up to read while an answer streams no longer pushes what you are reading; a Latest button takes you back.

## [1.0.8] - 2026-11-02

### Fixed
- The microphone no longer freezes the app.

## [1.0.7] - 2026-11-02

### Added
- Voice: tap the microphone beside the message box to speak instead of type.
