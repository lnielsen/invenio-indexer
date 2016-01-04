# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2016 CERN.
#
# Invenio is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.
#
# In applying this license, CERN does not
# waive the privileges and immunities granted to it by virtue of its status
# as an Intergovernmental Organization or submit itself to any jurisdiction.

"""API for indexing of records."""

from __future__ import absolute_import, print_function

from celery.messaging import establish_connection
from elasticsearch.helpers import bulk
from flask import current_app
from invenio_records.api import Record
from invenio_search import current_search_client
from invenio_search.utils import schema_to_index
from kombu import Producer
from kombu.compat import Consumer
from sqlalchemy.orm.exc import NoResultFound

from .signals import before_record_index


def _record_to_index(record):
    """Get index/doctype given a record."""
    index, doctype = schema_to_index(record.get('$schema', ''))
    if index and doctype:
        return index, doctype
    else:
        return current_app.config['INDEXER_DEFAULT_INDEX'], \
            current_app.config['INDEXER_DEFAULT_DOCTYPE']


class RecordIndexer(object):
    """Record indexer.

    Provides an interface for indexing records in Elasticsearch. Bulk indexing
    works by queuing requests for indexing records and processing these
    requests in bulk.

    Elasticsearch index and doctype for a record is determined from the
    ``$schema`` attribute.

    :param search_client: Elasticsearch client. Defaults to
        ``current_search_client``
    :param exchange: ``kombu.Exchange`` instance for message queue.
    :param queue: ``kombu.Queue`` instance for message queue.
    :param routing_key: Routing key for message queue.
    :param version_type: Elasticsearch version type. Defaults to
        ``external_gte``.
    """

    def __init__(self, search_client=None, exchange=None, queue=None,
                 routing_key=None, version_type=None, record_to_index=None):
        """Initialize indexer."""
        self.client = search_client or current_search_client
        self._exchange = None
        self._queue = None
        self._record_to_index = record_to_index or _record_to_index
        self._routing_key = None
        self._version_type = version_type or 'external_gte'

    @property
    def mq_queue(self):
        """Message queue queue."""
        return self._queue or current_app.config['INDEXER_MQ_QUEUE']

    @property
    def mq_exchange(self):
        """Message queue exchange."""
        return self._exchange or current_app.config['INDEXER_MQ_EXCHANGE']

    @property
    def mq_routing_key(self):
        """Message queue routing key."""
        return self._routing_key or \
            current_app.config['INDEXER_MQ_ROUTING_KEY']

    #
    # High-level API
    #
    def index(self, record):
        """Index a record.

        The caller is responsible for ensuring that the record has already been
        committed to the database. If a newer version of a record has already
        been indexed then the provided record will not be indexed. This
        behavior can be controlled by providing a different ``version_type``
        when initializing ``RecordIndexer``.

        :param record: Record instance.
        """
        index, doctype = self._record_to_index(record)

        return self.client.index(
            id=str(record.id),
            version=record.revision_id,
            version_type=self._version_type,
            index=index,
            doctype=doctype,
            body=self._prepare_record(record),
        )

    def index_by_id(self, record_uuid):
        """Index a record by record identifier.

        :param record_uuid: Record identifier.
        """
        return self.index(Record.get_record(record_uuid))

    def delete(self, record):
        """Delete a record.

        :param record: Record instance.
        """
        index, doctype = self._record_to_index(record)

        return self.client.delete(
            id=str(record.id),
            index=index,
            doctype=doctype,
        )

    def delete_by_id(self, record_uuid):
        """Delete record from index by record identifier."""
        self.delete(Record.get_record(record_uuid))

    def bulk_index(self, record_id_iterator):
        """Bulk index records.

        :param record_id_iterator: Iterator yielding record UUIDs.
        """
        self._bulk_op(record_id_iterator, 'index')

    def bulk_delete(self, record_id_iterator):
        """Bulk delete records from index.

        :param record_id_iterator: Iterator yielding record UUIDs.
        """
        self._bulk_op(record_id_iterator, 'delete')

    def process_bulk_queue(self):
        """Process bulk indexing queue."""
        with establish_connection() as conn:
            consumer = Consumer(
                connection=conn,
                queue=self.mq_queue.name,
                exchange=self.mq_exchange.name,
                routing_key=self.mq_routing_key,
            )

            count = bulk(
                self.client,
                self._actionsiter(consumer.iterqueue()),
                stats_only=True)

            consumer.close()

        return count

    #
    # Low-level implementation
    #
    def _bulk_op(self, record_id_iterator, op_type, index=None, doctype=None):
        """Index record in Elasticsearch asynchronously.

        :param record_id_iterator: Iterator that yields record UUIDs.
        :param op_type: Indexing operation (one of ``index``, ``create``,
            ``delete`` or ``update``).
        """
        assert op_type in ('index', 'create', 'delete', 'update')

        with establish_connection() as conn:
            producer = Producer(
                conn,
                exchange=self.mq_exchange,
                routing_key=self.mq_routing_key,
                auto_declare=True,
            )
            for rec in record_id_iterator:
                producer.publish(dict(
                    id=str(rec),
                    op=op_type,
                    index=index,
                    doctype=doctype
                ))

    def _actionsiter(self, message_iterator):
        """Iterate bulk actions.

        :param message_iterator: Iterator yielding messages from a queue.
        """
        for message in message_iterator:
            payload = message.decode()
            try:
                if payload['op'] == 'delete':
                    yield self._delete_action(payload)
                else:
                    yield self._index_action(payload)
                message.ack()
            except NoResultFound:
                message.reject()

    def _delete_action(self, payload):
        """Bulk delete action.

        :param payload: Decoded message body.
        :returns: Dictionary defining an Elasticsearch bulk 'delete' action.
        """
        if payload['index'] and payload['doctype']:
            index, doctype = payload['index'], payload['doctype']
        else:
            record = Record.get_record(payload['id'])
            index, doctype = self._record_to_index(record)

        return {
            '_op_type': 'delete',
            '_index': index,
            '_type': doctype,
            '_id': payload['id'],
        }

    def _index_action(self, payload):
        """Bulk index action.

        :param payload: Decoded message body.
        :returns: Dictionary defining an Elasticsearch bulk 'index' action.
        """
        record = Record.get_record(payload['id'])
        index, doctype = self._record_to_index(record)

        return {
            '_op_type': 'index',
            '_index': index,
            '_type': doctype,
            '_id': str(record.id),
            '_version': record.revision_id,
            '_version_type': self._version_type,
            '_source': self._prepare_record(record),
        }

    @staticmethod
    def _prepare_record(record):
        """Prepare record data for indexing."""
        data = record.dumps()

        # Allow modification of data prior to sending to Elasticsearch.
        before_record_index.send(
            current_app._get_current_object(),
            json=data,
            record=record,
        )

        return data
