package io.dlne.chat.data

/**
 * Whether a question is about where the person is, so that the phone's position is worth
 * fetching (and sending). Turning the GPS on for "write a poem" would cost battery and wait time
 * for nothing, and send a position nobody needs.
 *
 * The first alternatives are the server's own phrase match (`_NEAR_ME` in engine/search.py,
 * which adds the nearby town to a search); keep them the same. The rest are the cases the model
 * answers with the weather tool at the person's place, and the standing phrases for "where I am".
 * The server's model-set `near_me` flag can still catch a local question this misses; that
 * question then goes without a position and the app says so only when this matched.
 */
object LocalQuestion {
    private val PLACE = Regex(
        "\\b(?:near me|nearby|near here|around here|close to me|around me" + // = the server's _NEAR_ME
            "|my location|my current location|current location|my area|my city|my town|where i am|where i live)\\b",
        RegexOption.IGNORE_CASE,
    )
    private val WEATHER = Regex("\\b(?:weather|forecast|rain(?:ing)?|snow(?:ing)?)\\b", RegexOption.IGNORE_CASE)

    /**
     * A message about a picture: in "can I get a picture of a fox in the snow?" the snow is what to draw, not a question
     * about the weather where the person is (the owner's own first try asked for the location). A place phrase ("a
     * picture of the park near me") still counts. The web chat has the same rule (engine/chat_page.py).
     */
    private val PICTURE = Regex(
        "\\b(?:pictures?|images?|drawings?|paintings?|illustrations?|sketch(?:es)?|wallpapers?|draw|paint)\\b",
        RegexOption.IGNORE_CASE,
    )

    fun matches(text: String): Boolean =
        PLACE.containsMatchIn(text) || (WEATHER.containsMatchIn(text) && !PICTURE.containsMatchIn(text))

    /**
     * Whether the latest user message, or the one before it, is local. The one before it counts
     * so that "and tomorrow?" after "weather near me" still gets the position.
     */
    fun asks(messages: List<ChatMessage>): Boolean =
        messages.asReversed().filter { it.role == Role.USER && it.image == null }.take(2).any { matches(it.content) }

    /**
     * Whether sending [messages] (the new message last) must first ask the person to share their
     * location: the question is local, sharing is off, and this chat has not been answered "Not now"
     * (or refused by Android). With sharing on the position simply goes along; in a chat that was
     * answered, or for any other question, the message goes straight out. A new message with no words
     * (photos alone, or the text was deleted from the box) asks nothing, whatever came before it.
     */
    fun shouldAsk(messages: List<ChatMessage>, sharing: Boolean, declinedInChat: Boolean): Boolean =
        !sharing && !declinedInChat && messages.lastOrNull()?.content?.isNotBlank() == true && asks(messages)
}
