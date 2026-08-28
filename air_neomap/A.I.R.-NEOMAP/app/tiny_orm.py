"""
OFFLINE TEST SHIM ONLY -- not part of the app, not for deployment.

This sandbox has no network access, so flask_sqlalchemy can't be
pip installed here. This file is a minimal in-memory replacement
with just enough surface (db.Model, db.Column, .query.filter_by,
.query.get, session.add/delete/commit) for app/models.py and
app/engagement_logic.py to run without a single line changed, so
the consent-boundary tests below exercise real application logic.

It deliberately does NOT implement real SQL, real relationships,
real foreign key enforcement, or real joins -- get_attendance_trend
and get_visitor_counts fall back to simplified equivalents in the
test file itself rather than faking SQLAlchemy's join/group_by,
since faking those precisely would risk hiding bugs behind shim
bugs. Swap in the real flask_sqlalchemy (already listed in
requirements.txt) once you have pip access; nothing in app/ changes.
"""
import itertools


class Column:
    def __init__(self, *a, default=None, **k):
        self.default = default


class _Type:
    def __call__(self, *a, **k):
        return None


class Query:
    def __init__(self, cls, items=None):
        self.cls = cls
        self.items = items if items is not None else list(cls._store)
        self._order_key = None
        self._reverse = False
        self._limit = None

    def filter_by(self, **kw):
        items = [o for o in self.items if all(getattr(o, k, None) == v for k, v in kw.items())]
        return Query(self.cls, items)

    def filter(self, pred):
        items = [o for o in self.items if pred(o)]
        return Query(self.cls, items)

    def order_by(self, keyfunc_and_reverse):
        keyfunc, reverse = keyfunc_and_reverse
        q = Query(self.cls, list(self.items))
        q._order_key, q._reverse = keyfunc, reverse
        return q

    def limit(self, n):
        q = Query(self.cls, list(self.items))
        q._order_key, q._reverse, q._limit = self._order_key, self._reverse, n
        return q

    def _resolved(self):
        items = list(self.items)
        if self._order_key:
            items.sort(key=self._order_key, reverse=self._reverse)
        if self._limit:
            items = items[: self._limit]
        return items

    def all(self):
        return self._resolved()

    def first(self):
        r = self._resolved()
        return r[0] if r else None

    def get(self, id_):
        for o in self.cls._store:
            if getattr(o, "id", None) == id_:
                return o
        return None

    def count(self):
        return len(self.items)


class _QueryDescriptor:
    def __get__(self, obj, owner):
        return Query(owner)


class _OrderableField:
    """What Model.some_column resolves to at the CLASS level (e.g.
    CheckIn.date.desc()), distinct from instance.some_column which
    is the actual stored value. Real SQLAlchemy does this via
    InstrumentedAttribute; this is the same idea, minimal version."""
    def __init__(self, name):
        self.name = name

    def desc(self):
        return (lambda o: getattr(o, self.name), True)

    def asc(self):
        return (lambda o: getattr(o, self.name), False)

    def __eq__(self, other):
        # Supports Model.field == value used inside .filter(...),
        # returning a predicate rather than a bool, matching how
        # real SQLAlchemy's ColumnOperators.__eq__ builds a
        # BinaryExpression instead of comparing immediately.
        return lambda o: getattr(o, self.name) == other

    def __hash__(self):
        return hash(self.name)


class _ModelMeta(type):
    def __getattribute__(cls, name):
        # Only intercept declared Column attributes, and only when
        # accessed on the class itself (Model.field), never on an
        # instance (instance.field returns the real stored value
        # via normal instance __dict__ lookup, which takes priority
        # in Python regardless of this).
        raw = type.__getattribute__(cls, name)
        if isinstance(raw, Column):
            return _OrderableField(name)
        return raw


class Model(metaclass=_ModelMeta):
    query = _QueryDescriptor()

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls._store = []
        cls._ids = itertools.count(1)

    def __init__(self, **kw):
        cls = type(self)
        for name, val in vars(cls).items():
            if isinstance(val, Column) and name not in kw:
                d = val.default
                kw.setdefault(name, d() if callable(d) else d)
        for k, v in kw.items():
            setattr(self, k, v)
        if getattr(self, "id", None) is None:
            self.id = next(cls._ids)


class SQLAlchemy:
    Model = Model
    Column = Column

    def __init__(self):
        self.Integer = self.String = self.Boolean = self.Text = self.DateTime = self.Date = _Type()

    def ForeignKey(self, *a, **k):
        return None

    def relationship(self, *a, **k):
        return None

    def backref(self, *a, **k):
        return None

    def UniqueConstraint(self, *a, **k):
        return None

    def init_app(self, app):
        pass

    def create_all(self):
        pass

    def drop_all(self):
        for cls in Model.__subclasses__():
            cls._store = []
            cls._ids = itertools.count(1)

    class _Session:
        def add(self, obj):
            store = type(obj)._store
            if obj not in store:
                store.append(obj)

        def add_all(self, objs):
            for o in objs:
                self.add(o)

        def delete(self, obj):
            store = type(obj)._store
            if obj in store:
                store.remove(obj)

        def commit(self):
            pass

        def flush(self):
            pass

        def refresh(self, obj):
            pass

    session = _Session()