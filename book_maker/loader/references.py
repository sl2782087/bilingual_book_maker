"""Resolve local, explicitly identified EPUB notes and ruby without altering the DOM."""

import posixpath
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT

NOTE_TYPES = {"footnote", "endnote", "rearnote", "note"}
SKIP = {"script", "style", "rt", "rp", "rtc"}
MAX_NOTE_CHARS = 1000
MAX_NOTES = 4


def semantics(node):
    return set(str(node.get("epub:type", "")).split()) | set(
        role.removeprefix("doc-") for role in str(node.get("role", "")).split()
    )


def plain(node, *, readings=False):
    return " ".join(
        "".join(
            str(text)
            for text in node.find_all(string=True)
            if not any(
                parent.name in ({"script", "style", "rp"} if readings else SKIP)
                or "backlink" in semantics(parent)
                for parent in text.parents
            )
        ).split()
    )


class ReferenceIndex:
    def __init__(self, book):
        self.targets = {}
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            member = posixpath.normpath(item.file_name)
            soup = BeautifulSoup(item.content, "html.parser")
            for node in soup.find_all(attrs={"id": True}):
                key = (member, node["id"])
                self.targets[key] = None if key in self.targets else node

    def for_unit(self, unit):
        ruby_rows, note_rows, unresolved = [], [], []
        seen_ruby, seen_notes = set(), set()
        for text in unit.nodes or []:
            for ruby in text.parents:
                if ruby.name != "ruby" or id(ruby) in seen_ruby:
                    continue
                seen_ruby.add(id(ruby))
                if len(ruby_rows) >= 16:
                    if not any(row.get("reason") == "ruby_limit" for row in unresolved):
                        unresolved.append({"kind": "ruby", "reason": "ruby_limit"})
                    continue
                readings = [plain(node, readings=True) for node in ruby.find_all("rt")]
                base = plain(ruby)
                if base and readings:
                    ruby_rows.append(
                        {"base": base[:256], "readings": [s[:256] for s in readings]}
                    )
                    if len(base) > 256 or any(len(s) > 256 for s in readings):
                        unresolved.append(
                            {"kind": "ruby", "reason": "reference_truncated"}
                        )
        for link in unit.element.find_all("a", href=True):
            # A container's nested blocks are independent units, not this unit's notes.
            if any(
                parent.name in {"p", "div", "li", "aside", "blockquote", "section"}
                for parent in link.parents
                if parent is not unit.element and unit.element in parent.parents
            ):
                continue
            owned = {id(node) for node in unit.nodes or []}
            linked_to_run = any(id(node) in owned for node in link.descendants)
            linked_to_run |= any(
                link is marker or link in marker.descendants
                for marker in unit.markers.values()
            )
            if unit.owner_runs != 1 and not linked_to_run:
                if "noteref" in semantics(link):
                    unresolved.append({"href": link["href"], "reason": "ambiguous_run"})
                continue
            flags = semantics(link)
            if "backlink" in flags:
                continue
            href = link["href"]
            parsed = urlsplit(href)
            explicit = "noteref" in flags
            reason = None
            target = None
            member = (
                posixpath.normpath(
                    posixpath.join(
                        posixpath.dirname(unit.file_name), unquote(parsed.path)
                    )
                )
                if parsed.path
                else posixpath.normpath(unit.file_name)
            )
            if (
                parsed.scheme
                or parsed.netloc
                or parsed.query
                or member.startswith(("/", "../"))
            ):
                reason = "nonlocal_or_invalid_reference"
            elif not parsed.fragment:
                reason = "missing_fragment"
            else:
                target = self.targets.get((member, unquote(parsed.fragment)))
                if target is None:
                    reason = "missing_or_duplicate_target"
            if reason:
                if explicit:
                    unresolved.append({"href": href, "reason": reason})
                continue
            if not explicit and not (semantics(target) & NOTE_TYPES):
                continue
            key = f"{member}#{unquote(parsed.fragment)}"
            if key in seen_notes:
                continue
            seen_notes.add(key)
            if len(note_rows) >= MAX_NOTES:
                unresolved.append({"href": href, "reason": "note_limit"})
                continue
            text = plain(target)
            if not text:
                unresolved.append({"href": href, "reason": "empty_note"})
                continue
            note_rows.append({"target": key, "source": text[:MAX_NOTE_CHARS]})
            if len(text) > MAX_NOTE_CHARS:
                unresolved.append({"href": href, "reason": "reference_truncated"})
        result = {}
        if ruby_rows:
            result["ruby"] = ruby_rows
        if note_rows:
            result["notes"] = note_rows
        if unresolved:
            result["unresolved"] = unresolved
        return result
