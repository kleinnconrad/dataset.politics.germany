"""
Registry of the parties and politicians of the dataset.

Parties are a fixed table in this module: code, name, aliases, the ids used by
DAWUM and candidate titles of the German Wikipedia article. Politicians are
read from abgeordnetenwatch by the parliament collector and kept in
``state/politicians.json``; the annotation resolves the names returned by the
model against this registry, so that only politicians with a mandate are stored.
"""
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from state import atomic_write_text, dump_entries, load_entries

logger = logging.getLogger(__name__)

POLITICIANS_FILE = "politicians.json"
NO_FACTION = "NONE"


@dataclass(frozen=True)
class Party:
    """A party of the registry.

    Attributes:
        code (str): Party code used in all datasets.
        name (str): Official name.
        aliases (tuple[str, ...]): Other spellings, abbreviations and group names.
        dawum_ids (tuple[str, ...]): Party ids at DAWUM.
        wikipedia_titles (tuple[str, ...]): Candidate titles of the German
            Wikipedia article, resolved to the current title at run time.
    """
    code: str
    name: str
    aliases: tuple[str, ...] = ()
    dawum_ids: tuple[str, ...] = ()
    wikipedia_titles: tuple[str, ...] = ()


PARTIES: tuple[Party, ...] = (
    Party("CDU_CSU", "CDU/CSU", ("Union", "Unionsparteien", "CDU/CSU-Fraktion", "CDU/CSU-Bundestagsfraktion"), ("1",)),
    Party("CDU", "Christlich Demokratische Union Deutschlands", ("Christlich Demokratische Union",), ("101",),
          ("Christlich Demokratische Union Deutschlands",)),
    Party("CSU", "Christlich-Soziale Union in Bayern", ("Christlich-Soziale Union",), ("102",),
          ("Christlich-Soziale Union in Bayern",)),
    Party("SPD", "Sozialdemokratische Partei Deutschlands", ("Sozialdemokraten",), ("2",),
          ("Sozialdemokratische Partei Deutschlands",)),
    Party("GRUENE", "Bündnis 90/Die Grünen", ("Grüne", "Die Grünen", "B90/Grüne", "Bündnis 90/Grüne", "Gruene"),
          ("4",), ("Bündnis 90/Die Grünen",)),
    Party("FDP", "Freie Demokratische Partei", ("Liberale", "FDP/DVP"), ("3",), ("Freie Demokratische Partei",)),
    Party("AFD", "Alternative für Deutschland", ("AfD",), ("7",), ("Alternative für Deutschland",)),
    Party("LINKE", "Die Linke", ("Linke", "Linkspartei", "DIE LINKE", "Gruppe Die Linke"), ("5",), ("Die Linke",)),
    Party("BSW", "Bündnis Soziale Gerechtigkeit und Wirtschaftliche Vernunft",
          ("Bündnis Sahra Wagenknecht", "Bündnis Sahra Wagenknecht – Vernunft und Gerechtigkeit"), ("23",),
          ("Bündnis Soziale Gerechtigkeit und Wirtschaftliche Vernunft", "Bündnis Sahra Wagenknecht")),
    Party("FW", "Freie Wähler", ("FREIE WÄHLER", "Freie Wähler Bayern"), ("8",),
          ("Freie Wähler (Partei)", "Freie Wähler")),
    Party("SSW", "Südschleswigscher Wählerverband", (), ("10",), ("Südschleswigscher Wählerverband",)),
    Party("VOLT", "Volt Deutschland", ("Volt",), ("18",), ("Volt Deutschland",)),
    Party("PIRATEN", "Piratenpartei Deutschland", ("Piraten", "Piratenpartei"), ("6",)),
    Party("OEDP", "Ökologisch-Demokratische Partei", ("ÖDP",), ("12",)),
    Party("TIERSCHUTZ", "Partei Mensch Umwelt Tierschutz", ("Tierschutzpartei",), ("15",)),
    Party("DIE_PARTEI", "Die PARTEI", (), ("13",)),
    Party("BP", "Bayernpartei", (), ("11",)),
    Party("NPD", "Die Heimat (früher NPD)", ("NPD", "Die Heimat"), ("9",)),
    Party("BVB_FW", "Brandenburger Vereinigte Bürgerbewegungen/Freie Wähler", ("BVB/FW",), ("14",)),
    Party("BIW", "Bürger in Wut", (), ("16",)),
    Party("FAMILIE", "Familienpartei Deutschlands", ("Familienpartei",), ("17",)),
    Party("WERTEUNION", "WerteUnion", (), ("25",)),
    Party("PLUS_BB", "Plus Brandenburg", (), ("24",)),
    Party("BUNT_SAAR", "bunt.saar", (), ("21",)),
    Party("BFTH", "Bürger für Thüringen", (), ("22",)),
    Party("OTHER", "Sonstige", ("sonstige Parteien",), ("0",)),
)

PARTY_BY_CODE: dict[str, Party] = {party.code: party for party in PARTIES}
PARTY_CODE_BY_DAWUM_ID: dict[str, str] = {
    dawum_id: party.code for party in PARTIES for dawum_id in party.dawum_ids
}


def _normalize_label(label: str) -> str:
    """Normalizes a party or group label for lookup: no parentheses, case and dash differences."""
    without_parentheses = re.sub(r"\([^)]*\)", " ", label)
    collapsed = " ".join(without_parentheses.replace("–", "-").split())
    return collapsed.casefold()


_PARTY_CODE_BY_LABEL: dict[str, str] = {}
for _party in PARTIES:
    for _label in (_party.code, _party.name, *_party.aliases):
        _PARTY_CODE_BY_LABEL.setdefault(_normalize_label(_label), _party.code)
for _label, _code in (("cdu/csu", "CDU_CSU"), ("bündnis 90/die grünen", "GRUENE"), ("die linke.", "LINKE"),
                      ("fraktionslos", NO_FACTION), ("parteilos", NO_FACTION)):
    _PARTY_CODE_BY_LABEL[_label] = _code


def resolve_party(label: Optional[str]) -> Optional[str]:
    """Returns the party code for a party or parliamentary group label.

    Args:
        label: For example "BÜNDNIS 90/DIE GRÜNEN", "CDU/CSU (Bundestag 2025 - 2029)" or "fraktionslos".

    Returns:
        Optional[str]: The party code, NONE for members without party or group, or None if unknown.
    """
    if not label:
        return None
    return _PARTY_CODE_BY_LABEL.get(_normalize_label(label))


def normalize_person_name(name: str) -> str:
    """Normalizes a person name for matching: no academic titles, accents, case or extra spaces.

    Args:
        name: For example "Dr. Jürgen Müller-Lüdenscheidt".

    Returns:
        str: For example "jurgen muller-ludenscheidt".
    """
    without_titles = re.sub(r"\b(Dr|Prof|Dipl|Ing|med|phil|rer|nat|h\.\s*c)\.", " ", name)
    decomposed = unicodedata.normalize("NFKD", without_titles.replace("ß", "ss"))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


class Mandate(BaseModel):
    """A current mandate of a politician.

    Attributes:
        parliament (str): Parliament label at abgeordnetenwatch, e.g. "Bundestag".
        period (str): Electoral period label, e.g. "Bundestag 2025 - 2029".
        faction (Optional[str]): Parliamentary group as party code or NONE.
        constituency (Optional[str]): Constituency in which the politician stood, whether or not it was won.
        electoral_list (Optional[str]): Electoral list on which the politician stood.
        start_date (Optional[str]): Start of the mandate (YYYY-MM-DD).
        end_date (Optional[str]): End of the mandate (YYYY-MM-DD).
    """
    parliament: str
    period: str
    faction: Optional[str] = None
    constituency: Optional[str] = None
    electoral_list: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class Politician(BaseModel):
    """A politician of the registry, keyed by the abgeordnetenwatch id.

    Attributes:
        name (str): Full name.
        first_name (Optional[str]): First name.
        last_name (Optional[str]): Last name.
        party (Optional[str]): Party code, NONE without party, or None if unknown.
        sex (Optional[str]): female, male or diverse.
        year_of_birth (Optional[int]): Year of birth.
        wikidata_qid (Optional[str]): Wikidata id.
        mandates (list[Mandate]): Current mandates.
    """
    name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    party: Optional[str] = None
    sex: Optional[str] = None
    year_of_birth: Optional[int] = None
    wikidata_qid: Optional[str] = None
    mandates: list[Mandate] = Field(default_factory=list)


def load_politicians(state_dir: Path) -> dict[str, Politician]:
    """Reads the politician registry; a missing file means an empty registry.

    Args:
        state_dir: Folder of the state files.

    Returns:
        dict[str, Politician]: Politicians by abgeordnetenwatch id.
    """
    entries = load_entries(state_dir / POLITICIANS_FILE, "politicians")
    return {key: Politician.model_validate(value) for key, value in entries.items()}


def save_politicians(state_dir: Path, politicians: dict[str, Politician]) -> None:
    """Writes the politician registry atomically.

    Args:
        state_dir: Folder of the state files.
        politicians: Politicians by abgeordnetenwatch id.
    """
    atomic_write_text(
        state_dir / POLITICIANS_FILE,
        dump_entries("politicians", {key: value.model_dump(mode="json", exclude_none=True)
                                     for key, value in politicians.items()}),
    )


class PoliticianIndex:
    """Looks up politicians of the registry by the names that appear in texts."""

    def __init__(self, politicians: dict[str, Politician]) -> None:
        """Builds the lookup tables.

        Args:
            politicians: Politicians by abgeordnetenwatch id.
        """
        self._by_full_name: dict[str, set[str]] = defaultdict(set)
        self._by_last_name: dict[str, set[str]] = defaultdict(set)
        self._party: dict[str, Optional[str]] = {}
        for politician_id, politician in politicians.items():
            self._party[politician_id] = politician.party
            self._by_full_name[normalize_person_name(politician.name)].add(politician_id)
            if politician.first_name and politician.last_name:
                full = f"{politician.first_name} {politician.last_name}"
                self._by_full_name[normalize_person_name(full)].add(politician_id)
            last_name = politician.last_name or politician.name.split()[-1]
            self._by_last_name[normalize_person_name(last_name)].add(politician_id)

    def __len__(self) -> int:
        return len(self._party)

    def resolve(self, name: str, party: Optional[str] = None) -> Optional[str]:
        """Returns the id of the politician a name refers to, if it is unambiguous.

        A full-name match is used first. A last-name match is used only if the
        party given by the model selects exactly one politician.

        Args:
            name: The name as written in the text.
            party: Party code suggested by the model, or None.

        Returns:
            Optional[str]: The abgeordnetenwatch id, or None if unknown or ambiguous.
        """
        normalized = normalize_person_name(name)
        candidates = self._by_full_name.get(normalized, set())
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1 and party:
            matching = {pid for pid in candidates if self._matches_party(pid, party)}
            return next(iter(matching)) if len(matching) == 1 else None
        if candidates or not party or not normalized:
            return None
        last_name = normalized.split()[-1]
        matching = {pid for pid in self._by_last_name.get(last_name, set()) if self._matches_party(pid, party)}
        return next(iter(matching)) if len(matching) == 1 else None

    def _matches_party(self, politician_id: str, party: str) -> bool:
        """Tells whether a politician belongs to a party; CDU_CSU matches CDU and CSU members."""
        own = self._party.get(politician_id)
        if own is None:
            return False
        if party == "CDU_CSU" or own == "CDU_CSU":
            return own in ("CDU", "CSU", "CDU_CSU") and party in ("CDU", "CSU", "CDU_CSU")
        return own == party
