"""What the agents and the reader are told.

The independent variable of this experiment is FORMAT. ROLE, COMMON and
READER_SYSTEM are control variables: they must stay byte-identical across the
three conditions, or a difference in the results cannot be attributed to the
message format.

FORMAT is taken verbatim from the week-04 lecture notes (PART B, acl.py).
"""

# Buyer and seller are deliberately symmetric: same sentence count, same
# instructions, mirrored comparisons. An asymmetry here would surface in the
# results looking like a format effect.
ROLE = {
    "buyer": (
        "You are the buyer of {item}. You are negotiating the price with the seller. "
        "Your private limit: you can pay at most {limit}. "
        "Never agree to a price above {limit}, and do not state the number {limit} "
        "to the seller. A deal at or below {limit} is better for you than no deal, "
        "and a lower price is better than a higher one."
    ),
    "seller": (
        "You are the seller of {item}. You are negotiating the price with the buyer. "
        "Your private limit: you can accept at least {limit}. "
        "Never agree to a price below {limit}, and do not state the number {limit} "
        "to the buyer. A deal at or above {limit} is better for you than no deal, "
        "and a higher price is better than a lower one."
    ),
}

# The four acts, from the FIPA Communicative Act Library. query-ref and cfp are
# not in this vocabulary, so an opening question has no act to map to. That gap
# is one of the things this lab measures, so it is not patched here.
#
# The turn limit IS stated. Hiding it was tried first and every episode ran to
# `open`: rejecting costs a greedy agent nothing when the clock is invisible, so
# a seller whose reserve is 40 rejected 100, 110, 115, 118 and 119 in a row
# rather than close (see the smoke run in this file's history). A deadline gives
# both sides a reason to converge. It is the same number in all three
# conditions, so it does not touch the independent variable.
COMMON = (
    " Four acts are available: propose (offer a price), accept-proposal (agree to "
    "the other side's last price, which ends the negotiation with a deal), "
    "reject-proposal (decline the last price and keep negotiating), refuse (leave "
    "the negotiation for good, no deal). Every message you send is exactly one of "
    "these four acts. Send accept-proposal only in reply to a price the other side "
    "has already named. When you do not accept the other side's price but still "
    "want to trade, reply with propose and your own price: reject-proposal "
    "carries no price, so a counter-offer has to be a propose. The negotiation "
    "stops after {max_turns} messages in total, counting both sides; if nobody "
    "has accepted by then there is no deal and neither of you gets anything."
)

# The one paragraph that changes. Verbatim from the lecture notes.
FORMAT = {
    "free": (
        " Write your message as one or two plain English sentences."
    ),
    "tagged": (
        " Start your message with exactly one performative tag in parentheses, one "
        "of (propose), (accept-proposal), (reject-proposal), (refuse), then write "
        "one plain English sentence."
    ),
    "structured": (
        ' Reply with exactly one JSON object and nothing else: '
        '{"performative": "propose" | "accept-proposal" | "reject-proposal" | '
        '"refuse", "content": {"price": <whole number or null>}}.'
    ),
}

# Design decision, carried by the sentence "You must pick one of these four".
# The reader gets no unclear/none escape hatch. An opening question therefore
# lands on one of the four acts -- usually refuse -- and the episode ends at
# turn 1. weeks/week-04/README.md line 42 names that as data about the four-act
# vocabulary, not a bug to hide.
#
# free and tagged share this prompt. tagged discards the performative field and
# keeps only the price; keeping the text identical makes it a control variable.
READER_SYSTEM = (
    "You are an observer reading a price negotiation between a buyer and a seller. "
    "Label the LAST message only. Reply with exactly one JSON object and nothing "
    "else, no prose and no code fences: "
    '{"performative": "propose" | "accept-proposal" | "reject-proposal" | "refuse", '
    '"price": <whole number or null>}. '
    "propose offers a price. accept-proposal agrees to the other side's last price "
    "and ends the negotiation with a deal. reject-proposal declines the last price "
    "and keeps negotiating. refuse leaves the negotiation for good. "
    "You must pick one of these four even when the message fits none of them. "
    "price is the price that the last message itself offers or agrees to, written "
    "as a whole number with no currency symbol; use null when the message names no "
    "such price."
)


def system_prompt(role: str, item: str, limit: int, condition: str,
                  max_turns: int) -> str:
    """role is 'buyer' or 'seller'; condition is 'free', 'tagged' or 'structured'.

    FORMAT is not passed through .format(): the structured paragraph is JSON and
    its braces have to survive.
    """
    return (ROLE[role].format(item=item, limit=limit)
            + COMMON.format(max_turns=max_turns)
            + FORMAT[condition])
