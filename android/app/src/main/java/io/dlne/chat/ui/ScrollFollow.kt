package io.dlne.chat.ui

/*
 * Following the answer (1.0.8). Owner: "if I scroll up on the answer to read something it constantly pushes what I'm
 * trying to read". The message list is reversed (item 0, the newest, sits at the bottom), so:
 *  - the list follows the newest message only while the reader left it at the newest end;
 *  - a reader scrolled up into the answer being written keeps the same words in view while it grows;
 *  - an answer finishing, or a picture starting, never pulls a reader who scrolled up back down. Sending does.
 */

/** The reversed list is at its newest end: item 0 with nothing of it scrolled off. */
internal fun atNewest(firstIndex: Int, firstOffset: Int): Boolean = firstIndex == 0 && firstOffset == 0

/**
 * Pixels to scroll forward so a growing item does not move what the reader is reading. A reversed list keeps its first
 * visible item at the same distance from that item's bottom edge, and an answer grows at its bottom edge, so without
 * this every new line slides the words up under the reader. Only when the reader is not following, the growing item
 * ([growingKey]) is the first visible one and partly scrolled off, and it grew.
 */
internal fun keepReaderInPlace(
    following: Boolean,
    firstKey: Any?,
    growingKey: Any,
    firstOffset: Int,
    oldSize: Int,
    newSize: Int,
): Int = if (!following && firstKey == growingKey && firstOffset > 0 && oldSize > 0 && newSize > oldSize) newSize - oldSize else 0

/**
 * Whether the list goes to the newest message after it changed: always when the reader just sent one ([justSent]);
 * otherwise only while following and not in the middle of a scroll the reader is making.
 */
internal fun jumpToNewest(following: Boolean, justSent: Boolean, scrolling: Boolean): Boolean =
    justSent || (following && !scrolling)
