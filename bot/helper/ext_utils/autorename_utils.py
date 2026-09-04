# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)
#
# Advanced template-based auto-rename. Extracts structured fields
# (title, season, episode, quality, year, group, codec, audio) from a
# messy filename and rebuilds it from a user template such as:
#   [MyGroup] {title} - S{season}E{episode} [{quality}]
# Uses parse-torrent-name (PTN) when it yields a good result, with a
# regex fallback so it still works on non-standard names.

from os import path as ospath
from re import IGNORECASE, compile as re_compile, sub as re_sub

from bot import LOGGER

_SEASON_EPISODE_PATTERNS = [
    (re_compile(r"\bS(\d{1,3})[\s._-]*(?:E|EP)[\s._-]*(\d{1,4})\b", IGNORECASE), True),
    (re_compile(r"\bSeason[\s._-]*(\d{1,3})[\s._-]*Episode[\s._-]*(\d{1,4})\b", IGNORECASE), True),
    (re_compile(r"\[S(\d{1,3})\]\[E(\d{1,4})\]", IGNORECASE), True),
    (re_compile(r"\b(\d{1,2})x(\d{1,3})\b"), True),  # 1x04
    (re_compile(r"\bS(\d{1,3})[\s._-]{1,3}(\d{1,4})\b", IGNORECASE), True),
    # episode-only: require a real E/EP/Episode marker so a bare year or
    # resolution number is never mistaken for an episode
    (re_compile(r"\b(?:E|EP|Episode)[\s._-]*(\d{1,4})\b", IGNORECASE), False),
    # anime style "Title - 12 (1080p)": a dash-delimited number that is
    # not a year and is followed by a quality/group tag
    (
        re_compile(
            r"\s-\s(\d{1,3})(?=\s*[\[(]|\s*$)(?<!\b(?:19|20)\d{2})", IGNORECASE
        ),
        False,
    ),
]

_QUALITY_PATTERNS = [
    (re_compile(r"\b(\d{3,4}[pi])\b", IGNORECASE), lambda m: m.group(1).lower()),
    (re_compile(r"\b(4k|2160p)\b", IGNORECASE), lambda m: "2160p"),
    (re_compile(r"\b(2k|1440p)\b", IGNORECASE), lambda m: "1440p"),
    (re_compile(r"\[(\d{3,4}[pi])\]", IGNORECASE), lambda m: m.group(1).lower()),
]

_YEAR_PATTERN = re_compile(r"\b(19|20)\d{2}\b")

# .part1.rar / .001 / .7z.002 — must be preserved so split parts stay
# groupable and in sequence during upload
_PART_SUFFIX = re_compile(
    r"(?:\.part\d+(?:\.\w+)?|(?:\.\w{1,4})?\.\d{3,})$", IGNORECASE
)


def _pad(value, width=2):
    return str(value).zfill(width) if value not in (None, "") else ""


def _regex_extract(filename):
    name = ospath.splitext(filename)[0]
    season = episode = None
    for pattern, has_season in _SEASON_EPISODE_PATTERNS:
        if match := pattern.search(name):
            if has_season:
                season, episode = match.group(1), match.group(2)
            else:
                episode = match.group(1)
            break

    quality = ""
    for pattern, extract in _QUALITY_PATTERNS:
        if match := pattern.search(name):
            quality = extract(match)
            break

    year = ""
    if match := _YEAR_PATTERN.search(name):
        year = match.group(0)

    return season, episode, quality, year


# everything from the first of these markers onward is metadata, not title
_TITLE_CUT = re_compile(
    r"[\s._-]*(?:"
    r"\bS\d{1,3}(?:[\s._-]*(?:E|EP)[\s._-]*\d{1,4})?\b"
    r"|\bSeason[\s._-]*\d{1,3}\b"
    r"|\b(?:E|EP|Episode)[\s._-]*\d{1,4}\b"
    r"|\b\d{1,2}x\d{1,3}\b"
    r"|\s-\s\d{1,3}(?=\s*[\[(]|\s*$)"
    r"|\b\d{3,4}[pi]\b|\b(?:4k|2k|uhd)\b"
    r"|\b(?:19|20)\d{2}\b"
    r"|\b(?:x264|x265|h\.?264|h\.?265|hevc|avc|web-?dl|webrip|bluray|blu-ray"
    r"|bdrip|brrip|hdrip|hdtv|dvdrip|hdtc|cam|ts|aac|ac3|dts|ddp?5\.?1"
    r"|10bit|8bit|hdr|esub|msub|dual|multi|imax)\b"
    r"|\[|\("
    r")",
    IGNORECASE,
)


def _clean_title(filename, season=None, episode=None, quality=None, year=None):
    title = ospath.splitext(filename)[0]
    # leading release-group tag: "[SubsPlease] Some Anime" -> "Some Anime"
    title = re_sub(r"^\s*[\[(][^\])]{0,40}[\])]\s*", " ", title)
    # cut at the first metadata marker rather than substring-replacing,
    # which would corrupt titles containing those letters (Love -> Lov)
    if match := _TITLE_CUT.search(title):
        title = title[: match.start()]
    title = re_sub(r"[._]+", " ", title)
    title = re_sub(r"\s{2,}", " ", title).strip(" -_.")
    return title


def extract_fields(filename):
    """Return a dict of fields for the rename template."""
    season = episode = quality = year = None
    title = None
    try:
        import PTN

        info = PTN.parse(filename)
        title = info.get("title") or None
        season = info.get("season")
        episode = info.get("episode")
        quality = info.get("resolution") or info.get("quality") or ""
        year = info.get("year") or ""
        if isinstance(season, list):
            season = season[0] if season else None
        if isinstance(episode, list):
            episode = episode[0] if episode else None
        if year:
            year = str(year)
        if quality:
            quality = str(quality).lower()
    except Exception:
        pass

    r_season, r_episode, r_quality, r_year = _regex_extract(filename)
    if season in (None, ""):
        season = r_season
    if episode in (None, ""):
        episode = r_episode
    if not quality:
        quality = r_quality
    if not year:
        year = r_year
    if not title:
        title = _clean_title(filename, r_season, r_episode, r_quality, r_year)

    return {
        "title": title or "",
        "season": _pad(season) if season not in (None, "") else "",
        "episode": _pad(episode) if episode not in (None, "") else "",
        "season_raw": str(season) if season not in (None, "") else "",
        "episode_raw": str(episode) if episode not in (None, "") else "",
        "quality": quality or "",
        "year": str(year) if year else "",
    }


def apply_autorename_template(filename, template):
    """Rebuild `filename` from `template`. Placeholders: {title} {season}
    {episode} {quality} {year} (also {season_raw}/{episode_raw} unpadded).
    Missing fields collapse cleanly (e.g. dangling 'S' / 'E' / separators
    are removed). Returns the original name on any failure."""
    if not template:
        return filename
    try:
        ext = ospath.splitext(filename)[1]
        # split/multipart markers must survive verbatim or the ordered
        # upload can no longer group and sequence the parts
        part_tag = ""
        if match := _PART_SUFFIX.search(filename):
            part_tag = match.group(0)
            if ext and part_tag.lower().endswith(ext.lower()) and len(part_tag) > len(ext):
                part_tag = part_tag[: -len(ext)]
            elif ext and part_tag.lower() == ext.lower():
                # the whole suffix IS the extension (e.g. ".002"); keep it
                # once, via ext, so it isn't emitted twice
                part_tag = ""
        fields = extract_fields(filename)

        # remember which placeholders resolved to nothing so their
        # decoration (the S/E letters, brackets, separators) goes too
        empty = {k for k, v in fields.items() if not v}
        result = template

        # a bracket/paren group whose only placeholders are empty is dropped
        def _drop_empty_group(match):
            inner = match.group(0)
            names = set(re_sub(r"[^a-z_{}]", "", inner).strip("{}").split("}{"))
            if names and names <= empty:
                return ""
            return inner

        result = re_sub(r"[\[(][^\[\]()]*\{[a-z_]+\}[^\[\]()]*[\])]",
                        _drop_empty_group, result, flags=IGNORECASE)

        # drop S/E letters attached to an empty season/episode
        if "season" in empty:
            result = re_sub(r"\bS\{season(?:_raw)?\}", "", result, flags=IGNORECASE)
        if "episode" in empty:
            result = re_sub(r"\bE(?:P)?\{episode(?:_raw)?\}", "", result, flags=IGNORECASE)

        for key, value in fields.items():
            result = result.replace("{" + key + "}", value)

        # drop any unresolved placeholders, then collapse the separators
        # and empty brackets they left behind
        result = re_sub(r"\{[a-z_]+\}", "", result, flags=IGNORECASE)
        result = re_sub(r"[\[({]\s*[\])}]", "", result)
        result = re_sub(r"\s+([\])}])", r"\1", result)
        # collapse separators orphaned between surviving parts
        result = re_sub(r"(?:\s*-\s*){2,}", " - ", result)
        result = re_sub(r"-\s*(?=[\[({])", "", result)
        result = re_sub(r"\s{2,}", " ", result)
        result = re_sub(r"[\s._-]*-[\s._-]*$", "", result)
        result = re_sub(r"^[\s._-]*-[\s._-]*", "", result)
        result = re_sub(r"\s{2,}", " ", result).strip(" -_.")

        if not result:
            return filename
        # don't double-append an extension the template already carries
        if ext and result.lower().endswith(ext.lower()):
            final = result if not part_tag else f"{result[: -len(ext)]}{part_tag}{ext}"
        else:
            final = f"{result}{part_tag}{ext}"
        LOGGER.info(f"Auto-rename: {filename} -> {final}")
        return final
    except Exception as e:
        LOGGER.error(f"Auto-rename failed for '{filename}': {e}")
        return filename
