"""Atomic supplemental facts, full old-value audit, and independent progress."""
from datetime import date
import json

from .config import default_end
from .datasets import DATASETS, ValidatedBatch


class ExtraStoreMixin:
    """Uses Store's connection, transaction boundary, and collection lifecycle."""

    def dataset_state(self, kind, scope):
        if kind not in DATASETS:
            raise ValueError(f"unknown dataset: {kind}")
        row = self.conn.execute('SELECT * FROM dataset_state WHERE kind=? AND scope=?',(kind,scope)).fetchone()
        return dict(row) if row is not None else None

    def _dataset_scope(self, collection):
        scope = DATASETS[collection['kind']].scope
        if scope == 'symbol':
            return collection['symbol']
        if scope == 'date':
            return collection['start_date']
        return 'SZ'

    def _archive_record(self, kind, spec, old, now, collection_id):
        from .storage import _json
        self.conn.execute('''INSERT INTO record_revisions(kind,symbol,row_key,old_json,replaced_at,replaced_by)
                             VALUES(?,?,?,?,?,?)''',
                          (kind,old['symbol'],_json([old[field] for field in spec.keys]),_json(dict(old)),now,collection_id))

    def complete_dataset(self, collection_id, batch):
        from .storage import _day, _json, _now, _symbol
        if not isinstance(batch, ValidatedBatch):
            raise TypeError('complete_dataset requires a ValidatedBatch')
        rows = [dict(row) for row in batch.rows]
        warnings = list(batch.warnings)
        if not all(isinstance(w,str) for w in warnings):
            raise TypeError('warnings must contain strings')
        counts = dict(inserted=0,updated=0,unchanged=0,removed=0)
        with self._transaction():
            collection = self._running(collection_id)
            kind = collection['kind']
            if kind not in DATASETS:
                raise ValueError('collection is not a supplemental dataset')
            spec = DATASETS[kind]
            fields = (*spec.fields,'raw_json')
            seen = set()
            for row in rows:
                if set(row) != set(fields):
                    raise ValueError('supplemental row fields do not match its dataset contract')
                _symbol(row['symbol'])
                if spec.scope=='symbol' and row['symbol']!=collection['symbol']:
                    raise ValueError('supplemental row symbol does not match request')
                for field in fields:
                    if row[field] is not None and not isinstance(row[field],str):
                        raise TypeError(f'{field} must be string or null')
                    if field.endswith('_date'):
                        _day(row[field])
                if row['raw_json'] is None:
                    raise ValueError('raw_json is required')
                # Check standard JSON; source NaN must have been represented as null/text.
                json.dumps(json.loads(row['raw_json']), allow_nan=False)
                key = tuple(row[field] for field in spec.keys)
                if any(value is None for value in key) or key in seen:
                    raise ValueError('missing or duplicate supplemental primary key')
                seen.add(key)
                if kind in ('valuation','suspension'):
                    if not collection['start_date'] <= row['trade_date'] <= collection['end_date']:
                        raise ValueError('supplemental date outside requested interval')
                if kind=='valuation' and row['trade_date'] > default_end().isoformat():
                    raise ValueError('valuation date is not a completed Shanghai day')
                if kind=='delisting' and row['coverage']!='SZ':
                    raise ValueError('fixed delisting interface covers SZ only')
                if kind=='suspension':
                    from .suspension import classify_suspension
                    details = json.loads(row['suspension_details_json'])
                    if not isinstance(details, list) or not details:
                        raise ValueError('suspension details must be a nonempty source-event array')
                    expected = classify_suspension(details, date.fromisoformat(row['trade_date']), default_end())
                    if row['suspension_status'] != expected:
                        raise ValueError('suspension status must match explicit source date evidence')
            if not rows:
                self._finish(collection,counts,warnings,empty=True)
                return counts
            now = _now()
            where = ' AND '.join(f'{field}=?' for field in spec.keys)
            columns = (*fields,'is_current','source','updated_at','collection_id')
            sql = f"INSERT INTO {spec.table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT({','.join(spec.keys)}) DO UPDATE SET "
            sql += ','.join(f'{field}=excluded.{field}' for field in columns if field not in spec.keys)
            for row in rows:
                old = self.conn.execute(f'SELECT * FROM {spec.table} WHERE {where}',tuple(row[field] for field in spec.keys)).fetchone()
                if old is not None and old['is_current']==1 and all(old[field]==row[field] for field in fields):
                    counts['unchanged']+=1
                    continue
                if old is None:
                    counts['inserted']+=1
                else:
                    counts['updated']+=1
                    self._archive_record(kind,spec,old,now,collection_id)
                self.conn.execute(sql,tuple(row[field] for field in fields)+(1,spec.interface,now,collection_id))
            # Full available dividend snapshots and per-day suspension snapshots
            # can withdraw previous observations. Preserve them and archive the
            # old row; disappearance means unknown, NOT cancellation/resumption.
            if kind in ('dividends','suspension'):
                clause, scope = ('symbol',collection['symbol']) if kind=='dividends' else ('trade_date',collection['start_date'])
                old_rows = self.conn.execute(f'SELECT * FROM {spec.table} WHERE {clause}=? AND is_current=1',(scope,)).fetchall()
                for old in old_rows:
                    key = tuple(old[field] for field in spec.keys)
                    if key in seen:
                        continue
                    self._archive_record(kind,spec,old,now,collection_id)
                    self.conn.execute(f'UPDATE {spec.table} SET is_current=0,updated_at=?,collection_id=? WHERE {where}',(now,collection_id,*key))
                    counts['removed']+=1
            if counts['removed']:
                warnings.append('observations absent from the latest nonempty snapshot were retired; cancellation or resumption is not inferred')
            self._advance_dataset(collection,now)
            self._finish(collection,counts,warnings,empty=False)
        return counts

    def _advance_dataset(self, collection, now):
        kind=collection['kind']
        spec=DATASETS[kind]
        scope=self._dataset_scope(collection)
        old=self.dataset_state(kind,scope)
        normal_start=old['normal_start'] if old else None
        normal_end=old['normal_end'] if old else None
        if kind=='valuation' and collection['mode']=='normal':
            effective_end=min(collection['end_date'],default_end().isoformat())
            if normal_end is None:
                normal_start=collection['start_date']
                normal_end=effective_end
            elif effective_end>normal_end and date.fromisoformat(collection['start_date']).toordinal()<=date.fromisoformat(normal_end).toordinal()+1:
                normal_end=effective_end
        latest=None
        if spec.date_field:
            if spec.scope=='symbol':
                clause,parameters='symbol=?',(collection['symbol'],)
            elif spec.scope=='date':
                clause,parameters='trade_date=?',(collection['start_date'],)
            else:
                clause,parameters='1=1',()
            latest=self.conn.execute(f'SELECT MAX({spec.date_field}) FROM {spec.table} WHERE {clause}',parameters).fetchone()[0]
        self.conn.execute('''INSERT INTO dataset_state(kind,scope,normal_start,normal_end,latest_data_date,last_success_id,updated_at)
                             VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,scope) DO UPDATE SET
                             normal_start=excluded.normal_start,normal_end=excluded.normal_end,
                             latest_data_date=excluded.latest_data_date,last_success_id=excluded.last_success_id,
                             updated_at=excluded.updated_at''',
                          (kind,scope,normal_start,normal_end,latest,collection['id'],now))
