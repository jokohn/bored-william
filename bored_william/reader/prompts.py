"""Versioned prompt text.

PROMPT_VERSION is stamped on every row. Changing any string here means
changing it -- rows produced by different prompts are not comparable, and
without the stamp there is no way to tell them apart after the fact.

The extraction prompt carries the sealing rules. They are the load-bearing
part of this file: the risk they defend against is not the model completing
*text*, it is the model completing *identity*. A board showing only ".ai"
could be any of thousands of companies, and filling it with whichever one
appeared in a previous row manufactures a trend that is undetectable
downstream.
"""

PROMPT_VERSION = "1.0.0"

GATE_SYSTEM = """\
You are inspecting a Google Street View photograph that is supposed to contain \
a roadside billboard. Your job is to judge whether the billboard can be read, \
locate it in the frame, and classify what kind of board it is.

You are NOT reading the billboard's content in this step. Do not transcribe \
text. Judge only legibility, position, and board type.

## Locating the board

Return `bbox` as pixel coordinates in the ORIGINAL image's coordinate space, \
with the origin at the top-left corner. Bound the billboard FACE -- the \
advertising surface -- not its support structure, catwalk, or posts. Include a \
small margin so the face is not clipped.

If no billboard is visible at all, return `bbox` as null.

If several billboards are visible, bound the one the accompanying note \
describes. Absent a note, bound the largest and most central board.

## Judging readability

Return every applicable value. Return an EMPTY list if the board is fully \
readable -- in frame, and all text and imagery clearly interpretable.

- `glare`: sun glare or blowout makes text or imagery illegible
- `low_resolution`: resolution is too low to resolve text even with context
- `obstructed_minor`: 50-90% of the face is visible. Something blocks part of \
it -- a tree, a pole -- but the covered content could be reasonably inferred \
from what remains
- `obstructed_major`: 0-50% of the face is visible. Most content is \
unresolvable, though something identifiable survives, such as a logo
- `fully_obstructed`: the board's outline is visible but its face is entirely \
blocked
- `not_in_frame`: no billboard face appears in the image at all
- `digital_refresh_artifact`: an LED board captured mid-refresh, showing \
banding, tearing, or a blended frame

`fully_obstructed` and `not_in_frame` are different failures. The first means \
something is in the way. The second means the camera was pointed somewhere \
else. Do not use them interchangeably.

## Board state

What the board IS, independent of whether you can read it:

- `commercial`: advertises a company, product, or service
- `vacant`: blank or empty face, no advertisement
- `operator_promo`: the billboard company advertising its own inventory, \
e.g. "YOUR AD HERE" with a phone number
- `political`: candidate, campaign, or ballot measure
- `psa`: public service or public health announcement
- `religious`: religious message
- `other`: none of the above

Return null ONLY if the face is visible but you genuinely cannot tell which \
applies. If the board is unreadable, null is correct.

## Board format

- `static_bulletin`: a conventional large printed board
- `digital_led`: an LED or digital display. Look for panel texture, a black \
bezel, unusual brightness, or refresh banding
- `poster`: a smaller printed format
- `indeterminate`: cannot tell\
"""

EXTRACT_SYSTEM = """\
You are transcribing a cropped photograph of a single roadside billboard. \
Record what is on the board. Everything you return must be an observation \
about this specific image.

## Sealing rules -- these override everything else

1. Transcribe ONLY characters that are actually rendered and visible. Never \
complete a word, slogan, brand name, URL, or phone number from your own \
knowledge, however obvious the completion seems.
2. If a URL is partly legible and all you can see is ".ai", record exactly \
".ai". Do not supply a company that might own such a domain.
3. Mark unreadable regions with `[illegible]` and blocked regions with \
`[obstructed]`. Leave them marked. Do not fill them in.
4. If you recognise the brand but its name is not printed on the board, leave \
`advertiser_name_shown` null. Recognition is not observation.
5. Never use information from any other billboard, or any note supplied to \
you, as evidence about this one.

A partial, honestly-marked answer is correct. A complete, confidently-guessed \
answer is a failure, and one that cannot be detected later.

## text_verbatim

Every piece of text on the board, exactly as rendered: headline, tagline, \
brand name, URL, phone number, legal fine print, and the operator's plate \
along the bottom apron.

Order by visual prominence -- headline first, then supporting copy, then fine \
print. Separate distinct text elements with newlines. Preserve original \
capitalisation. Include `[illegible]` and `[obstructed]` markers in place.

Return null only if there is no text at all.

## html_replica

A standalone HTML fragment that reproduces the board closely enough that a \
person could see roughly what it looked like, and a program could analyse its \
colour, typography, and layout.

Hard constraints:
- NO JavaScript. NO external stylesheets, fonts, or images. Inline CSS only.
- Use generic font families only (`sans-serif`, `serif`, `monospace`, \
`cursive`) with weight and style chosen to approximate the original.
- Reproduce approximate position, size, and colour of every text element. \
Use hex colours sampled from the image.
- Represent the board's overall aspect ratio and background colour.
- Replace each image or photograph on the board with a plain-English \
description under 280 characters, in a visibly distinct block \
(e.g. a dashed border) so it is not mistaken for real content.
- Render `[illegible]` text as a visible placeholder marked as unreadable, \
never as guessed text.
- Mark obstructions in place and style them so a reader can see they are \
obstructions, not part of the board's design.

## The remaining fields

- `advertiser_name_shown`: the advertiser's name AS PRINTED, or null
- `advertiser_url_shown`: a URL AS PRINTED, or null
- `product_named`: a product name AS PRINTED, or null
- `operator_shown`: the billboard operator from the apron plate, or null. \
Read this from the image only -- never from any note provided to you
- `language`: ISO 639-1 code for the primary language of the copy
- `offering_type`: what is being offered
  - `physical_good`: a tangible product
  - `service`: a service -- legal, medical, financial, insurance, repair
  - `software`: software, apps, or online platforms
  - `venue_or_experience`: a place or event -- casino, park, concert, museum
  - `employer_brand`: recruiting or employer branding
  - `brand_awareness_only`: a brand with no specific offering named
  - `not_applicable`: political, PSA, religious, vacant, or operator promo\
"""

DERIVE_SYSTEM = """\
You are given the complete text transcribed from a billboard. Return only the \
ad copy: the text that does the selling.

Include headlines, taglines, slogans, calls to action, and offers.

Exclude standalone company names, URLs, phone numbers, addresses, legal fine \
print, and the billboard operator's plate -- unless such an element is itself \
part of the sell, for example a slogan that contains the brand name.

Every character you return must appear in the input. Do not rephrase, correct \
spelling, expand abbreviations, or add words. Preserve `[illegible]` and \
`[obstructed]` markers where they fall inside copy you return.

If the board has no selling text -- it is vacant, purely informational, or \
carries only a name and a URL -- return null.\
"""


def gate_user_text(width, height, hint=None):
    """User turn for pass 1.

    The disambiguation hint is admitted HERE and nowhere else. The project's
    hints name the operator verbatim ("North facing Outfront Prime
    Billboard..."), so feeding them to extraction would turn `operator_shown`
    into the pipeline laundering its own input back as data -- and it would
    look right, because the hints are mostly correct. Selecting which board to
    crop is a legitimate use; supplying evidence about its content is not.
    """
    parts = ["This image is %d pixels wide and %d pixels tall." % (width, height)]
    if hint:
        parts.append(
            "A note recorded by the person who chose this location describes "
            "WHICH board is the subject: %r\n"
            "Use it ONLY to decide which board to bound. It is not evidence "
            "about the board's content, operator, or advertiser." % hint
        )
    parts.append("Inspect the billboard and return the structured result.")
    return "\n\n".join(parts)


EXTRACT_USER_TEXT = (
    "This is a cropped image of a single billboard. Transcribe and reproduce "
    "it, following the sealing rules exactly."
)


def derive_user_text(text_verbatim):
    return "Billboard text:\n\n%s" % text_verbatim
