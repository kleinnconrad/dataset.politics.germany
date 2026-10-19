"""
Defines the Pydantic models of the German politics dataset.

The models that Gemini fills (``ItemAnnotation`` and its parts) are separate
from the records written to disk (subclasses of ``RecordBase``).
Vocabularies are ``Literal`` aliases, because google-genai drops the field
descriptions of ``Enum`` classes when it builds the response schema. All
fields of the models sent to Gemini are required; absence is expressed with
"Not applicable", an empty list or null.
"""
import types
import typing
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"

# ISO 3166-2 codes of Germany and its states
StateCode = Literal["DE-BW", "DE-BY", "DE-BE", "DE-BB", "DE-HB", "DE-HH", "DE-HE", "DE-MV", "DE-NI", "DE-NW",
                    "DE-RP", "DE-SL", "DE-SN", "DE-ST", "DE-SH", "DE-TH"]
Jurisdiction = Literal["DE", StateCode]

# All party codes of the dataset. CDU_CSU is the joint Union (federal polls, parliamentary group);
# CDU and CSU are used where the single party is meant.
PartyCode = Literal["CDU_CSU", "CDU", "CSU", "SPD", "GRUENE", "FDP", "AFD", "LINKE", "BSW", "FW", "SSW", "VOLT",
                    "PIRATEN", "OEDP", "TIERSCHUTZ", "DIE_PARTEI", "BP", "NPD", "BVB_FW", "BIW", "FAMILIE",
                    "WERTEUNION", "PLUS_BB", "BUNT_SAAR", "BFTH", "OTHER"]
# The parties the model may name; all other parties are OTHER
AnnotationParty = Literal["CDU_CSU", "CDU", "CSU", "SPD", "GRUENE", "FDP", "AFD", "LINKE", "BSW", "FW", "SSW",
                          "VOLT", "OTHER"]

Parliament = Literal["Bundestag", "Landtag", "European Parliament"]
PollMethod = Literal["Telephone", "Online", "Telephone and online", "Face to face", "Face to face and online",
                     "Unknown"]

OutletType = Literal["public_broadcaster_national", "public_broadcaster_regional", "national_daily",
                     "weekly_magazine", "regional_daily", "tabloid", "online_only", "business"]
PaywallModel = Literal["free", "mixed", "hard"]
ActorType = Literal["federal_government", "federal_ministry", "bundestag", "bundesrat", "parliamentary_group",
                    "party", "state_government", "court"]
ContentBasis = Literal["title_only", "title_teaser", "lead_text"]
Channel = Literal["rss", "news_sitemap"]
ItemDataset = Literal["news_items", "press_releases"]

# Major topics of the Comparative Agendas Project (master codebook), plus Other
CapTopic = Literal["Macroeconomics", "Civil rights and liberties", "Health", "Agriculture", "Labor", "Education",
                   "Environment", "Energy", "Immigration", "Transportation", "Law and crime", "Social welfare",
                   "Housing", "Domestic commerce", "Defense", "Technology", "Foreign trade",
                   "International affairs", "Government operations", "Public lands and water", "Culture", "Other"]
EventType = Literal["Election campaign", "Election result", "Poll report", "Coalition conflict",
                    "Coalition formation", "Government decision", "Legislation", "Parliamentary debate or vote",
                    "Resignation or dismissal", "Appointment", "Scandal or investigation", "Court ruling",
                    "Protest or strike", "Party leadership change", "Intra-party conflict", "Party convention",
                    "Foreign or security crisis", "Economic development", "Other"]
EventStatus = Literal["Announced or planned", "Ongoing", "Concluded", "Not applicable"]
PoliticalLevel = Literal["Municipal", "State", "Federal", "European Union", "International", "Not political"]
ArticleType = Literal["News", "Opinion", "Interview", "Analysis", "Live blog", "Explainer", "Press release",
                      "Other"]
Tone = Literal["Positive", "Neutral", "Negative", "Mixed"]
GovernmentTone = Literal["Positive", "Neutral", "Negative", "Mixed", "Not applicable"]
# Generic news frames of Semetko and Valkenburg (2000)
Frame = Literal["Conflict", "Economic consequences", "Responsibility", "Human interest", "Morality",
                "No dominant frame"]
ActorRole = Literal["Initiator", "Target", "Commentator"]
CommunicationType = Literal["Policy proposal", "Position statement", "Reaction to event", "Attack on opponent",
                            "Achievement claim", "Announcement or appointment", "Event or service information",
                            "Other", "Not applicable"]
PopulismMarker = Literal["Anti-elite", "People-centric", "Exclusionary"]
VoteChoice = Literal["yes", "no", "abstain", "no_show"]
ProcedureChange = Literal["new", "status_changed", "updated"]


# ---------------------------------------------------------------------------
# Models filled by Gemini
# ---------------------------------------------------------------------------

class PartyMention(BaseModel):
    """A party that the text refers to, as filled by the model."""
    party: AnnotationParty = Field(description="Code of the party. CDU or CSU for the single party, CDU_CSU for the Union as a whole or its parliamentary group. OTHER for parties not in the list.")
    role: ActorRole = Field(description="Initiator if the party acts or proposes; Target if it is criticized, attacked or affected; Commentator if it only comments.")
    tone: Tone = Field(description="How the text presents this party.")
    quoted: bool = Field(description="True if a representative of the party is quoted directly or indirectly.")


class PersonMention(BaseModel):
    """A politician that the text refers to, as filled by the model."""
    name: str = Field(description="Full name as written in the text. Only politicians, office holders and candidates; no journalists, experts or private persons.")
    party: Optional[AnnotationParty] = Field(description="Party of the person if stated or well known; null otherwise.")
    role: ActorRole = Field(description="Initiator, Target or Commentator, as for parties.")
    tone: Tone = Field(description="How the text presents this person.")


class ConflictPair(BaseModel):
    """Two parties that the text reports in dispute with each other."""
    party_a: AnnotationParty = Field(description="First party of the dispute.")
    party_b: AnnotationParty = Field(description="Second party of the dispute.")


class UpcomingEvent(BaseModel):
    """A future event that the text names with a date."""
    event_type: EventType = Field(description="Type of the upcoming event.")
    date: str = Field(description="Date of the event (YYYY-MM-DD). Only events with a stated day.")


class ItemAnnotation(BaseModel):
    """The labels of one article or press release."""
    item_number: int = Field(description="Number of the item in the request, starting at 1.")
    is_political: bool = Field(description="True if the item is about politics, government, parties, elections, legislation, public administration or political conflicts.")
    political_level: PoliticalLevel = Field(description="Main political level of the item. 'Not political' if is_political is false.")
    states_concerned: list[StateCode] = Field(description="German states the item is mainly about (ISO 3166-2). Empty list for federal or international items.")
    cap_topic: CapTopic = Field(description="Main policy topic (Comparative Agendas Project major topics).")
    cap_topics_secondary: list[CapTopic] = Field(description="Further clearly addressed policy topics, at most two. Empty list if none.")
    issue_detail: Optional[str] = Field(description="The specific issue in one to five words, e.g. 'Pension level' or 'Debt brake reform'. Null if not political.")
    article_type: ArticleType = Field(description="Form of the item, judged from the title and text, e.g. 'Opinion' for commentaries, 'Interview' for interviews.")
    event_types: list[EventType] = Field(description="Types of political events the item reports. Empty list if none.")
    event_status: EventStatus = Field(description="Whether the main event is announced or planned, ongoing or concluded. 'Not applicable' without an event.")
    government_involved: bool = Field(description="True if the federal government or a state government, or one of their members, acts or is addressed.")
    parties: list[PartyMention] = Field(description="Every party the item refers to, each once. Members and parliamentary groups count for their party.")
    politicians: list[PersonMention] = Field(description="Politicians, office holders and candidates named in the item, each once.")
    overall_tone: Tone = Field(description="Overall tone of the item.")
    government_tone: GovernmentTone = Field(description="How the item presents the federal government. 'Not applicable' if it is not addressed.")
    frame: Frame = Field(description="Dominant frame of the item.")
    conflict_pairs: list[ConflictPair] = Field(description="Pairs of parties that the item reports in dispute with each other. Empty list if none.")
    reports_poll: bool = Field(description="True if the item reports results of an opinion poll.")
    poll_institute: Optional[str] = Field(description="Name of the polling institute if a poll is reported; null otherwise.")
    upcoming_events: list[UpcomingEvent] = Field(description="Future political events named with a date (votes, deadlines, conventions, summits, elections). Empty list if none.")
    communication_type: CommunicationType = Field(description="For press releases: the main purpose of the release. 'Not applicable' for news articles.")
    populism_markers: list[PopulismMarker] = Field(description="For press releases: populist rhetoric present in the text (Anti-elite: hostility toward elites or the establishment; People-centric: appeals to 'the people' as a homogeneous group; Exclusionary: exclusion of out-groups). Empty list for news articles and if none.")


class AnnotationBatch(BaseModel):
    """The model response for a batch of items."""
    items: list[ItemAnnotation] = Field(description="One entry per item of the request, in the order of the request.")


# ---------------------------------------------------------------------------
# Stored records
# ---------------------------------------------------------------------------

class RecordBase(BaseModel):
    """Fields that every record of the dataset has."""
    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(description="Stable id of the item. Together with revision it identifies a record.")
    revision: int = Field(default=0, description="0 for the first observation; incremented each time the source changes the item.")
    dataset: str = Field(description="Name of the dataset.")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Version of the record schema.")
    observed_at: datetime = Field(description="UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t.")
    source_id: str = Field(description="Id of the source (API, outlet or press source).")
    source_url: Optional[str] = Field(description="URL of the item or of the API resource it was read from.")
    license: str = Field(description="License or terms of the source.")
    jurisdiction: Jurisdiction = Field(description="DE for the federal level, ISO 3166-2 code for a state.")
    backfilled: bool = Field(description="True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public.")


class PollRecord(RecordBase):
    """One published opinion poll (Sonntagsfrage)."""
    poll_id: str = Field(description="Id of the poll at DAWUM.")
    parliament: Parliament = Field(description="Parliament the poll asks about.")
    institute: str = Field(description="Polling institute.")
    tasker: Optional[str] = Field(description="Client that commissioned the poll.")
    method: PollMethod = Field(description="Survey method.")
    fieldwork_start: Optional[date] = Field(description="First day of the fieldwork.")
    fieldwork_end: Optional[date] = Field(description="Last day of the fieldwork.")
    published_date: date = Field(description="Publication date of the poll.")
    sample_size: Optional[int] = Field(description="Number of respondents.")
    results: dict[str, float] = Field(description="Vote intention in percent by party code, without the others.")
    others_share: Optional[float] = Field(description="Share of all other parties in percent.")


class PageviewRecord(RecordBase):
    """Daily views of the German Wikipedia article of a party or politician."""
    entity_id: str = Field(description="Party code, or politician id at abgeordnetenwatch.")
    entity_type: Literal["party", "politician"] = Field(description="Type of the entity.")
    wikipedia_title: str = Field(description="Title of the article in the German Wikipedia.")
    wikidata_qid: Optional[str] = Field(description="Wikidata id of the entity.")
    daily_views: dict[date, int] = Field(description="Views by human users per day (UTC), for the days since the previous run.")


class NewsItemRecord(RecordBase):
    """Metadata of one news article. The text is not stored."""
    outlet_id: str = Field(description="Id of the outlet.")
    outlet_type: OutletType = Field(description="Type of the outlet.")
    owner_group: str = Field(description="Publisher group of the outlet, or 'public' for public broadcasters.")
    home_state: Optional[StateCode] = Field(description="State of a regional outlet; null for national outlets.")
    channel: Channel = Field(description="Where the article was found: RSS/Atom feed or news sitemap.")
    url: str = Field(description="URL of the article.")
    canonical_url: Optional[str] = Field(description="Canonical URL declared by the article page.")
    title: str = Field(description="Title of the article.")
    published_at: Optional[datetime] = Field(description="Publication time (UTC).")
    updated_at: Optional[datetime] = Field(description="Last modification time from the article page (UTC).")
    section: Optional[str] = Field(description="Section of the outlet, from the page, the feed or the URL path.")
    feed_categories: list[str] = Field(description="Categories of the item in the feed.")
    keywords: list[str] = Field(description="Keywords from the article page or the news sitemap.")
    jsonld_type: Optional[str] = Field(description="schema.org type of the article page, e.g. NewsArticle or OpinionNewsArticle.")
    is_agency_copy: Optional[bool] = Field(description="True if the article is credited to a news agency (dpa, AFP, Reuters, epd, KNA, AP). Null without page data.")
    author_count: Optional[int] = Field(description="Number of named authors. Author names are not stored. Null without page data.")
    word_count: Optional[int] = Field(description="Word count from the article page. Null if not given.")
    is_paywalled: Optional[bool] = Field(description="True if the page marks the article as not accessible for free. Null without page data.")
    has_video: Optional[bool] = Field(description="True if the article page embeds a video. Null without page data.")
    content_basis: ContentBasis = Field(description="Text available for the annotation: title only, title and teaser, or lead text of the page.")


class PressReleaseRecord(RecordBase):
    """Metadata of one press release. The text is not stored."""
    press_source_id: str = Field(description="Id of the press source.")
    actor_type: ActorType = Field(description="Type of the issuing actor.")
    party: Optional[PartyCode] = Field(description="Party of a party or parliamentary group; null for other actors.")
    url: str = Field(description="URL of the press release.")
    canonical_url: Optional[str] = Field(description="Canonical URL declared by the page.")
    title: str = Field(description="Title of the press release.")
    published_at: Optional[datetime] = Field(description="Publication time (UTC).")
    feed_categories: list[str] = Field(description="Categories of the item in the feed.")
    content_basis: ContentBasis = Field(description="Text available for the annotation.")


class TextAnnotationRecord(RecordBase):
    """Gemini labels of one news article or press release."""
    item_dataset: ItemDataset = Field(description="Dataset of the annotated item.")
    item_id: str = Field(description="record_id of the annotated item.")
    model: str = Field(description="Gemini model that produced the labels.")
    extraction_hash: str = Field(description="Hash of the prompt, response schema and model settings. Labels with the same hash were produced under the same conditions.")
    content_basis: ContentBasis = Field(description="Text the model saw: title only, title and teaser, or lead text.")
    is_political: bool = Field(description="Whether the item is about politics.")
    political_level: PoliticalLevel = Field(description="Main political level of the item.")
    states_concerned: list[StateCode] = Field(description="States the item is mainly about.")
    cap_topic: CapTopic = Field(description="Main policy topic (Comparative Agendas Project).")
    cap_topics_secondary: list[CapTopic] = Field(description="Further policy topics.")
    issue_detail: Optional[str] = Field(description="Specific issue in a few words.")
    article_type: ArticleType = Field(description="Form of the item.")
    event_types: list[EventType] = Field(description="Types of political events reported.")
    event_status: EventStatus = Field(description="Stage of the main event.")
    government_involved: bool = Field(description="Whether a government or its members act or are addressed.")
    mentioned_parties: list[AnnotationParty] = Field(description="Parties the item refers to.")
    party_roles: dict[str, ActorRole] = Field(description="Role of each mentioned party.")
    party_tone: dict[str, Tone] = Field(description="Tone toward each mentioned party.")
    quoted_parties: list[AnnotationParty] = Field(description="Parties whose representatives are quoted.")
    mentioned_politicians: list[str] = Field(description="abgeordnetenwatch ids of the named politicians that are in the entity registry.")
    politician_tone: dict[str, Tone] = Field(description="Tone toward each resolved politician, by abgeordnetenwatch id.")
    unresolved_person_count: int = Field(description="Number of named persons that are not in the entity registry. Their names are not stored.")
    overall_tone: Tone = Field(description="Overall tone of the item.")
    government_tone: GovernmentTone = Field(description="Tone toward the federal government.")
    frame: Frame = Field(description="Dominant news frame (Semetko and Valkenburg).")
    conflict_pairs: list[list[str]] = Field(description="Pairs of parties reported in dispute, each pair sorted.")
    reports_poll: bool = Field(description="Whether poll results are reported.")
    poll_institute: Optional[str] = Field(description="Polling institute of a reported poll.")
    upcoming_events: list[dict[str, str]] = Field(description="Future events with event_type and date (YYYY-MM-DD).")
    communication_type: CommunicationType = Field(description="Purpose of a press release; 'Not applicable' for news.")
    populism_markers: list[PopulismMarker] = Field(description="Populist rhetoric in a press release.")


class RollCallVoteRecord(RecordBase):
    """One roll-call vote (namentliche Abstimmung) in the Bundestag."""
    vote_id: str = Field(description="Id of the vote at abgeordnetenwatch.")
    parliament_period: str = Field(description="Electoral period, e.g. 'Bundestag 2025 - 2029'.")
    vote_date: date = Field(description="Date of the vote.")
    title: str = Field(description="Title of the vote.")
    topics: list[str] = Field(description="Topics assigned by abgeordnetenwatch.")
    committees: list[str] = Field(description="Committees in charge.")
    printed_papers: list[str] = Field(description="Numbers of the Bundestag printed papers (Drucksachen) linked in the description of the vote, e.g. '21/7869'. They link the vote to procedures in the DIP (filter f.dokumentnummer).")
    accepted: Optional[bool] = Field(description="Whether the motion was accepted.")
    yes: int = Field(description="Number of yes votes.")
    no: int = Field(description="Number of no votes.")
    abstain: int = Field(description="Number of abstentions.")
    no_show: int = Field(description="Number of members who did not vote.")
    by_faction: dict[str, dict[str, int]] = Field(description="Counts of yes, no, abstain and no_show by parliamentary group at the time of the vote (party code, NONE for members without a group).")
    cohesion: dict[str, Optional[float]] = Field(description="Rice index |yes - no| / (yes + no) per parliamentary group; null if the group cast no yes or no votes. Members without a group (NONE) are not included.")
    defectors: dict[str, int] = Field(description="Members per parliamentary group who voted yes while the group cast more no than yes votes, or no while it cast more yes than no votes. Members without a group (NONE) are not included.")


class MpVoteRecord(RecordBase):
    """The vote of one member in one roll-call vote."""
    vote_id: str = Field(description="Id of the vote at abgeordnetenwatch.")
    vote_date: date = Field(description="Date of the vote.")
    politician_id: str = Field(description="Id of the politician at abgeordnetenwatch.")
    faction: str = Field(description="Parliamentary group at the time of the vote as party code, NONE for members without a group.")
    vote: VoteChoice = Field(description="The member's vote.")
    voted_against_faction: bool = Field(description="True if the member voted yes while the group cast more no than yes votes, or no while it cast more yes than no votes. Always false for members without a group.")


class ProcedureRecord(RecordBase):
    """One parliamentary procedure (Vorgang) in the DIP, recorded when it appears or changes."""
    procedure_id: str = Field(description="Id of the procedure in the DIP.")
    change: ProcedureChange = Field(description="new for the first observation, status_changed if the status changed, updated for other tracked changes.")
    procedure_type: str = Field(description="Type of the procedure, e.g. Gesetzgebung, Antrag, Kleine Anfrage.")
    electoral_period: Optional[int] = Field(description="Electoral period of the Bundestag.")
    title: str = Field(description="Title of the procedure.")
    initiators: list[str] = Field(description="Initiators, e.g. Bundesregierung, Bundesrat or a parliamentary group.")
    subject_areas: list[str] = Field(description="Subject areas (Sachgebiete) of the DIP.")
    descriptors: list[str] = Field(description="Subject descriptors of the DIP thesaurus.")
    consent_required: Optional[bool] = Field(description="Whether the consent of the Bundesrat is required, from the latest statement of the DIP (bill, Bundestag decision or promulgation); null if not stated.")
    status: Optional[str] = Field(description="Current status (Beratungsstand).")
    previous_status: Optional[str] = Field(description="Status at the previous observation; null for new procedures.")
    gesta_id: Optional[str] = Field(description="GESTA id of a bill.")
    dip_updated_at: Optional[datetime] = Field(description="Last update of the procedure in the DIP (UTC).")
    latest_document_date: Optional[date] = Field(description="Date of the latest document of the procedure (DIP field datum), e.g. of the printed paper or plenary record that set the current status.")


class PoliticianRecord(RecordBase):
    """A politician with a current mandate, recorded when the entry appears or changes."""
    politician_id: str = Field(description="Id of the politician at abgeordnetenwatch.")
    name: str = Field(description="Full name.")
    party: Optional[Literal[PartyCode, "NONE"]] = Field(description="Current party as party code; NONE without party; OTHER for parties not in the registry; null if unknown.")
    sex: Optional[Literal["female", "male", "diverse"]] = Field(description="Sex as stated by abgeordnetenwatch.")
    year_of_birth: Optional[int] = Field(description="Year of birth.")
    wikidata_qid: Optional[str] = Field(description="Wikidata id.")
    mandates: list[dict[str, Optional[str]]] = Field(description="Current mandates with parliament, period, faction (party code, NONE without group), constituency and electoral_list the member stood in (null if none), mandate_won (constituency, list or moved_up), start_date and end_date (YYYY-MM-DD; null if equal to the start or end of the period).")


DATASET_MODELS: dict[str, type[RecordBase]] = {
    "polls": PollRecord,
    "wikipedia_pageviews": PageviewRecord,
    "news_items": NewsItemRecord,
    "press_releases": PressReleaseRecord,
    "text_annotations": TextAnnotationRecord,
    "roll_call_votes": RollCallVoteRecord,
    "mp_votes": MpVoteRecord,
    "parliamentary_procedures": ProcedureRecord,
    "politicians": PoliticianRecord,
}

# Models whose text form goes into the extraction hash
ANNOTATION_MODELS: tuple[type[BaseModel], ...] = (
    PartyMention, PersonMention, ConflictPair, UpcomingEvent, ItemAnnotation, AnnotationBatch,
)


def _describe_type(annotation: object) -> str:
    """Returns a text form of a type annotation that is the same on all supported Python versions.

    Args:
        annotation: A type annotation of a model field.

    Returns:
        str: For example ``list[PartyMention]`` or ``Union[NoneType,str]``.
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is Literal:
        return "Literal[" + ",".join(repr(arg) for arg in args) + "]"
    if origin is typing.Union or origin is types.UnionType:
        return "Union[" + ",".join(sorted(_describe_type(arg) for arg in args)) + "]"
    if origin is list:
        return "list[" + _describe_type(args[0]) + "]"
    if origin is dict:
        return "dict[" + _describe_type(args[0]) + "," + _describe_type(args[1]) + "]"
    if isinstance(annotation, type):
        return annotation.__name__
    return repr(annotation)


def describe_models(models: typing.Iterable[type[BaseModel]], include_descriptions: bool) -> str:
    """Returns a canonical text form of models.

    The text lists every field with its type and whether it is required. It is
    used for the extraction hash (annotation models, with descriptions) and for
    the schema snapshot test (all models, without descriptions).

    Args:
        models: The models to describe.
        include_descriptions: Whether to include the field descriptions.

    Returns:
        str: One line per model and per field.
    """
    lines = []
    for model in models:
        lines.append(model.__name__)
        for name, field in model.model_fields.items():
            line = f"  {name}: {_describe_type(field.annotation)} required={field.is_required()}"
            if include_descriptions:
                line += f" description={field.description!r}"
            lines.append(line)
    return "\n".join(lines)
