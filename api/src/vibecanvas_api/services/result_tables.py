"""Bounded, versioned result-table indexes. Invoke outside the event loop."""
from collections import OrderedDict, Counter
from copy import deepcopy
import json
import math
import sys
from threading import RLock

from fastapi import HTTPException

from .batch_evaluation import load_results
from .object_store import get_task_result_store, uri_to_key


def _size(value):
    pending, seen, size = [value], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        size += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return size


class ResultTableCache:
    def __init__(self, max_bytes=16 * 1024 * 1024, max_entries=8):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = RLock()

    def read(self, scope, uri):
        store, key = get_task_result_store(), uri_to_key(uri)
        # Serialize cache fills to bound simultaneous JSON decoding and avoid
        # a burst of requests each loading the same file. All work is threaded.
        with self.lock:
            try:
                revision = store.revision(key)
                cache_key = (*scope, uri, revision)
                if cache_key in self.entries:
                    self.entries.move_to_end(cache_key)
                    return self.entries[cache_key][0]
                rows, version = load_results(uri, store=store)
                if store.revision(key) != revision:
                    raise HTTPException(409, 'Results changed during loading; retry the query.')
            except KeyError as exc:
                raise HTTPException(404, 'No result file is available.') from exc
            counts = Counter(row.get('status') for row in rows if isinstance(row.get('status'), str))
            index = (rows, tuple(json.dumps(row, ensure_ascii=False).casefold() for row in rows),
                     {name: counts[name] for name in ('success', 'error', 'cancelled')}, version)
            size = _size((cache_key, index)) + 256
            # Remove superseded revisions even when the replacement is too
            # large to cache. Authorization is still checked on every request.
            for old_key in list(self.entries):
                if old_key[:-1] == cache_key[:-1]:
                    self.bytes -= self.entries.pop(old_key)[1]
            if size <= self.max_bytes and self.max_entries > 0:
                while self.entries and (self.bytes + size > self.max_bytes or len(self.entries) >= self.max_entries):
                    self.bytes -= self.entries.popitem(last=False)[1][1]
                self.entries[cache_key] = (index, size)
                self.bytes += size
            return index


_cache = ResultTableCache()


def query_result_table(scope, uri, query):
    rows, search_text, counts, version = _cache.read(scope, uri)

    def cell(row, key):
        value = row.get(key)
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value if value is not None else '')

    needle = query.search.casefold()
    filters = [(key, value.casefold()) for key, value in query.filters.items()]
    selected = [row for row, text in zip(rows, search_text)
                if (not needle or needle in text)
                and (not query.row_status or row.get('status') == query.row_status)
                and all(value in cell(row, key).casefold() for key, value in filters)]
    numeric = query.sort in {'index', 'i', 'attempt', 'execution_time', 'elapsed_ms'}

    def sort_key(row):
        if not numeric:
            return cell(row, query.sort).casefold()
        try:
            value = float(row.get(query.sort) or 0)
            return value if math.isfinite(value) else 0
        except (ValueError, TypeError):
            return 0

    selected.sort(key=sort_key, reverse=query.descending)
    return {'rows': deepcopy(selected[query.offset:query.offset + query.limit]),
            'filtered': len(selected), 'total': len(rows), 'counts': dict(counts), 'version': version}
