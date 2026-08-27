"""Output schema, controlled vocabularies, and the structured-output models.

Every closed vocabulary lives here rather than in a prompt string. The model is
constrained to these values through structured outputs, so an off-vocabulary
answer fails at the API boundary instead of surfacing months later as a
one-off category in an analysis. "Be consistent" cannot work as an instruction
across independent per-image calls -- there is nothing for a call to be
consistent with -- so consistency has to be enforced by construction.

TAXONOMY_VERSION is stamped on every row. These lists will change, and a row
has to say which set it was scored against.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

TAXONOMY_VERSION = "1.0.0"
NAICS_VINTAGE = "2022"


class Readability(str, Enum):
    """Why some or all of the board could not be read.

    An empty list means fully readable. `fully_obstructed` and `not_in_frame`
    are deliberately separate: the first is a physical blockage that no amount
    of re-aiming fixes, the second is a targeting failure that a corrected
    --assumed-distance would recover. Collapsing them would hide a fixable
    systematic problem inside an unfixable one.
    """

    GLARE = "glare"
    LOW_RESOLUTION = "low_resolution"
    OBSTRUCTED_MINOR = "obstructed_minor"   # 50-90% visible; gaps are fillable
    OBSTRUCTED_MAJOR = "obstructed_major"   # 0-50% visible; something survives
    FULLY_OBSTRUCTED = "fully_obstructed"   # physically blocked
    NOT_IN_FRAME = "not_in_frame"           # aim failure
    DIGITAL_REFRESH_ARTIFACT = "digital_refresh_artifact"


class BoardState(str, Enum):
    """What the board *is*, as distinct from whether it could be read.

    `vacant` and `operator_promo` both mean unsold inventory economically, but
    they look different on the board, so they stay separate and roll up at
    analysis time. A collapsed field can never be un-collapsed.
    """

    COMMERCIAL = "commercial"
    VACANT = "vacant"
    OPERATOR_PROMO = "operator_promo"
    POLITICAL = "political"
    PSA = "psa"
    RELIGIOUS = "religious"
    OTHER = "other"


class BoardFormat(str, Enum):
    STATIC_BULLETIN = "static_bulletin"
    DIGITAL_LED = "digital_led"
    POSTER = "poster"
    INDETERMINATE = "indeterminate"


class OfferingType(str, Enum):
    """What is being offered, if anything.

    Chosen over a formal product taxonomy: a large share of corridor boards
    advertise no purchasable thing at all -- B2B software aimed at other
    companies' engineers, employer branding, pure brand awareness -- and
    forcing those into product codes yields low fill rates and strained
    classifications. `product_named` is kept as free text so a product
    taxonomy can be layered on later without re-running any image.
    """

    PHYSICAL_GOOD = "physical_good"
    SERVICE = "service"
    SOFTWARE = "software"
    VENUE_OR_EXPERIENCE = "venue_or_experience"
    EMPLOYER_BRAND = "employer_brand"
    BRAND_AWARENESS_ONLY = "brand_awareness_only"
    NOT_APPLICABLE = "not_applicable"


class BBox(BaseModel):
    """Billboard bounds in source-image pixels.

    Returned as coordinates, never as a cropped image -- the model emits text,
    and cropping locally keeps the operation deterministic, reproducible, and
    auditable from the recorded numbers.
    """

    x: int = Field(description="Left edge in pixels from the image's left")
    y: int = Field(description="Top edge in pixels from the image's top")
    w: int = Field(description="Width in pixels")
    h: int = Field(description="Height in pixels")


class GateResult(BaseModel):
    """Pass 1. Decides whether pass 2 is worth paying for."""

    readability: list[Readability] = Field(
        description="Empty list if the board is fully readable"
    )
    bbox: Optional[BBox] = Field(
        description="Bounds of the billboard face; null if no board is visible"
    )
    board_state: Optional[BoardState] = Field(
        description="What the board is; null only if genuinely indeterminate"
    )
    board_format: BoardFormat


class ExtractResult(BaseModel):
    """Pass 2. Observations only -- nothing here is an inference.

    This is the default shape. `html_replica` is absent: it is the single most
    expensive field in the schema, and most analyses never touch it. The
    variant carrying it is below, selected by --html.
    """

    text_verbatim: Optional[str] = Field(
        description="Every piece of text on the board, exactly as rendered"
    )
    advertiser_name_shown: Optional[str] = Field(
        description="Advertiser name as printed on the board, or null"
    )
    advertiser_url_shown: Optional[str] = Field(
        description="URL as printed on the board, or null"
    )
    product_named: Optional[str] = Field(
        description="Product name as printed on the board, or null"
    )
    offering_type: Optional[OfferingType]
    operator_shown: Optional[str] = Field(
        description="Billboard operator from the apron plate, or null"
    )
    language: Optional[str] = Field(
        description="Primary language of the copy as an ISO 639-1 code"
    )


class ExtractResultWithHtml(ExtractResult):
    """Extraction plus the HTML reproduction, selected by --html.

    Kept as a separate model rather than an optional field so the replica is
    genuinely absent from the output contract by default. A nullable field
    would still be part of the schema the model is asked to fill, which is
    most of the cost.
    """

    html_replica: Optional[str] = Field(
        description="Standalone HTML reproduction; no JS, no external assets"
    )


def extract_model(include_html=False):
    return ExtractResultWithHtml if include_html else ExtractResult


class DeriveResult(BaseModel):
    """Pass 3. Text-only; a filtered view of text_verbatim."""

    ad_copy: Optional[str] = Field(
        description="The selling text only, drawn verbatim from the input"
    )


# Column order for the per-image output table. `layer` is part of the schema,
# not documentation: an observation is checkable against the image by anyone,
# an inference is not, and the two deserve different weight from a reader.
COLUMNS = [
    "image_file",
    # gate
    "readability",
    "bbox",
    "board_state",
    "board_format",
    # extract
    "text_verbatim",
    "html_replica",
    "advertiser_name_shown",
    "advertiser_url_shown",
    "product_named",
    "offering_type",
    "operator_shown",
    "language",
    # derive
    "ad_copy",
    # provenance
    "gate_model",
    "extract_model",
    "prompt_version",
    "taxonomy_version",
    "extracted_at_utc",
    "status",
    "error_message",
]

LAYERS = {
    "image_file": "key",
    "readability": "observed", "bbox": "observed",
    "board_state": "observed", "board_format": "observed",
    "text_verbatim": "observed", "html_replica": "observed",
    "advertiser_name_shown": "observed", "advertiser_url_shown": "observed",
    "product_named": "observed", "offering_type": "observed",
    "operator_shown": "observed", "language": "observed",
    "ad_copy": "derived",
}

# Gate outcomes that prove there is nothing left to read. Anything else --
# including obstructed_major, where only a logo may survive -- still earns an
# extraction pass, because a partial row is worth more than no row.
NOTHING_TO_READ = {Readability.NOT_IN_FRAME, Readability.FULLY_OBSTRUCTED}
