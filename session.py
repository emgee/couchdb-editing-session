import copy
import itertools
import uuid
import UserDict
from peak.util.proxies import ObjectWrapper

import couchdb


class Session(object):

    def __init__(self, db):
        self._db = db
        self._cache = {}
        self._created = set()
        self._changed = set()
        self._deleted = {}

    #- Additional magic methods.

    def __getattr__(self, name):
        return getattr(self._db, name)

    #- Override couchdb.Database methods.

    def __iter__(self):
        # XXX Not entirely sure why we need this as all we're doing if passing
        # on the call but without it a magic method called __length_hint__ is
        # called, followed soon after by an exception from couchdb.
        return iter(self._db)

    def __len__(self):
        return len(self._db)

    def __delitem__(self, id):
        # XXX Really not sure this is a good way to delete a document, i.e.
        # without specifying the _rev, but I'll reluctantly support it because
        # the underlying database does.
        self.delete(self[id])

    def __getitem__(self, id):
        doc = self.get(id)
        if doc is None:
            raise couchdb.ResourceNotFound()
        return doc

    def __setitem__(self, id, content):
        # Ignore docs with a _rev, we should already be tracking changes to it
        # if it's been changed.
        if '_rev' in content:
            return
        doc = dict(content)
        doc['_id'] = id
        self.create(doc)

    def create(self, data):
        # XXX Whenever I see an object being copied I assume it's probably
        # wrong. However, in this case, I want the same semantics as the
        # underlying db's create to continue and be able to return the cached
        # document should it be asked for by ID.
        doc = copy.deepcopy(data)
        if '_id' not in doc:
            doc['_id'] = uuid.uuid4().hex
        self._created.add(doc['_id'])
        return self._cached(doc)['_id']

    def delete(self, doc):
        if doc['_id'] in self._created:
            self._created.remove(doc['_id'])
            del self._cache[doc['_id']]
        else:
            self._changed.discard(doc['_id'])
            self._deleted[doc['_id']] = doc['_rev']

    def get(self, id, default=None, **options):
        # Try cache first.
        doc = self._cache.get(id)
        if doc is not None:
            if doc['_id'] in self._deleted and doc['_id'] not in self._created:
                return None
            return doc
        # Ask CouchDB and cache the response (if found).
        doc = self._db.get(id, default, **options)
        if doc is default:
            return doc
        return self._cached(doc)

    def delete_attachment(self, doc, filename):
        raise NotImplementedError()

    def get_attachment(self, id_or_doc, filename, default=None):
        raise NotImplementedError()

    def put_attachment(self, doc, content, filename=None, content_type=None):
        raise NotImplementedError()

    def query(self, *a, **k):
        return SessionViewResults(self, self._db.query(*a, **k))

    def update(self, documents):
        raise NotImplementedError()

    def view(self, *a, **k):
        return SessionViewResults(self, self._db.view(*a, **k))

    #- Additional methods.

    def flush(self):
        # XXX Due to a bug in CouchDB (see issue COUCHDB-188) we can't do
        # deletions at the same time as additions if the list of updates
        # includes a delete and create for the same id. For now, let's keep
        # deletions out of the general updates list and make two calls to the
        # backend.
        # XXX We can't pass a generator to couchdb's Database.update.
        # XXX We can only pass dict instances in the list to couchdb's Database.update.
        # Build a list of deletions.
        deletions = [{'_id': id, '_rev': rev, '_deleted': True} for (id, rev) in self._deleted.iteritems()]
        # Build a list of other updates.
        additions = (dict(self._cache[doc_id]) for doc_id in self._created)
        changes = (dict(self._cache[doc_id]) for doc_id in self._changed)
        updates = list(itertools.chain(additions, changes))
        # Send deletions and clean up cache.
        self._db.update(deletions)
        for doc_id in self._deleted:
            if doc_id not in self._created:
                del self._cache[doc_id]
        self._deleted.clear()
        # Perform updates and fix up the cache with the new _revs.
        for response in self._db.update(updates):
            self._cache[response['_id']]['_rev'] = response['_rev']
        self._created.clear()
        self._changed.clear()

    def _cached(self, doc):
        def modified():
            self._changed.add(doc['_id'])
        doc = wrap(doc, modified)
        self._cache[doc['_id']] = doc
        return doc


class SessionViewResults(object):

    def __init__(self, session, view_results):
        self._session = session
        self._view_results = view_results

    def __getattr__(self, name):
        return getattr(self._view_results, name)

    def __len__(self):
        return len(self._view_results)

    def __getitem__(self, key):
        return SessionViewResults(self._session, self._view_results(key))

    def __iter__(self):
        for row in self._view_results:
            yield SessionRow(self._session, row)


class SessionRow(object):

    def __init__(self, session, row):
        self._session = session
        self._row = row

    def __getattr__(self, name):
        return getattr(self._row, name)

    def _get_doc(self):
        doc = self._row.doc
        if doc is not None:
            cached = self._session._cache.get(doc['_id'])
            if cached is not None:
                return cached
            return self._session._cached(doc)
    doc = property(_get_doc)


def wrap(obj, modified):
    if isinstance(obj, couchdb.Document):
        return Document(obj, modified)
    elif isinstance(obj, dict):
        return Dictionary(obj, modified)
    elif isinstance(obj, list):
        return List(obj, modified)
    return obj


class Dictionary(UserDict.DictMixin, ObjectWrapper):

    # TODO:
    #   __contains__(), __iter__(), and iteritems() to improve performance

    __modified = None

    def __init__(self, subject, modified):
        super(Dictionary, self).__init__(subject)
        self.__modified = modified

    def __getitem__(self, *a, **k):
        value = self.__subject__.__getitem__(*a, **k)
        return wrap(value, self.__modified)

    def __setitem__(self, *a, **k):
        self.__modified()
        return self.__subject__.__setitem__(*a, **k)

    def __delitem__(self, *a, **k):
        self.__modified()
        return self.__subject__.__delitem__(*a, **k)

    def keys(self, *a, **k):
        return self.__subject__.keys(*a, **k)


class Document(Dictionary):
    pass


class List(ObjectWrapper):

    __modified = None

    def __init__(self, subject, modified):
        super(List, self).__init__(subject)
        self.__modified = modified

    def __getitem__(self, *a, **k):
        value = self.__subject__.__getitem__(*a, **k)
        return wrap(value, self.__modified)
        
    def __getslice__(self, *a, **k):
        value = self.__subject__.__getslice__(*a, **k)
        return wrap(value, self.__modified)
        
    def __setitem__(self, *a, **k):
        self.__modified()
        return self.__subject__.__setitem__(*a, **k)

    def __delitem__(self, *a, **k):
        self.__modified()
        return self.__subject__.__delitem__(*a, **k)

    def __setslice__(self, *a, **k):
        self.__modified()
        return self.__subject__.__setslice__(*a, **k)

    def __delslice__(self, *a, **k):
        self.__modified()
        return self.__subject__.__delslice__(*a, **k)

    def append(self, *a, **k):
        self.__modified()
        return self.__subject__.append(*a, **k)

    def extend(self, *a, **k):
        self.__modified()
        return self.__subject__.extend(*a, **k)

    def insert(self, *a, **k):
        self.__modified()
        return self.__subject__.insert(*a, **k)

    def pop(self, *a, **k):
        self.__modified()
        return self.__subject__.pop(*a, **k)

    def remove(self, *a, **k):
        self.__modified()
        return self.__subject__.remove(*a, **k)

