# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from twisted.internet import defer

from synapse.events.utils import prune_event

from synapse.crypto.event_signing import check_event_content_hash

from synapse.api.errors import SynapseError

from synapse.util import unwrapFirstError

import logging


logger = logging.getLogger(__name__)


class FederationBase(object):
    @defer.inlineCallbacks
    def _check_sigs_and_hash_and_fetch(self, origin, pdus, outlier=False):
        """Takes a list of PDUs and checks the signatures and hashs of each
        one. If a PDU fails its signature check then we check if we have it in
        the database and if not then request if from the originating server of
        that PDU.

        If a PDU fails its content hash check then it is redacted.

        The given list of PDUs are not modified, instead the function returns
        a new list.

        Args:
            pdu (list)
            outlier (bool)

        Returns:
            Deferred : A list of PDUs that have valid signatures and hashes.
        """

        signed_pdus = []

        deferreds = self._check_sigs_and_hashes(pdus)

        def callback(pdu):
            signed_pdus.append(pdu)

        def errback(failure, pdu):
            failure.trap(SynapseError)

            # Check local db.
            new_pdu = yield self.store.get_event(
                pdu.event_id,
                allow_rejected=True,
                allow_none=True,
            )
            if new_pdu:
                signed_pdus.append(new_pdu)
                return

            # Check pdu.origin
            if pdu.origin != origin:
                try:
                    new_pdu = yield self.get_pdu(
                        destinations=[pdu.origin],
                        event_id=pdu.event_id,
                        outlier=outlier,
                        timeout=10000,
                    )

                    if new_pdu:
                        signed_pdus.append(new_pdu)
                        return
                except:
                    pass

            logger.warn(
                "Failed to find copy of %s with valid signature",
                pdu.event_id,
            )

        for pdu, deferred in zip(pdus, deferreds):
            deferred.addCallbacks(callback, errback, errbackArgs=[pdu])

        yield defer.gatherResults(
            deferreds,
            consumeErrors=True
        ).addErrback(unwrapFirstError)

        defer.returnValue(signed_pdus)

    def _check_sigs_and_hash(self, pdu):
        return self._check_sigs_and_hashes([pdu])[0]

    def _check_sigs_and_hashes(self, pdus):
        """Throws a SynapseError if a PDU does not have the correct
        signatures.

        Returns:
            FrozenEvent: Either the given event or it redacted if it failed the
            content hash check.
        """

        redacted_pdus = [
            prune_event(pdu)
            for pdu in pdus
        ]

        deferreds = self.keyring.verify_json_objects_for_server([
            (p.origin, p.get_pdu_json())
            for p in redacted_pdus
        ])

        def callback(_, pdu, redacted):
            if not check_event_content_hash(pdu):
                logger.warn(
                    "Event content has been tampered, redacting %s: %s",
                    pdu.event_id, pdu.get_pdu_json()
                )
                return redacted
            return pdu

        def errback(failure, pdu):
            failure.trap(SynapseError)
            logger.warn(
                "Signature check failed for %s",
                pdu.event_id,
            )
            return failure

        for deferred, pdu, redacted in zip(deferreds, pdus, redacted_pdus):
            deferred.addCallbacks(
                callback, errback,
                callbackArgs=[pdu, redacted],
                errbackArgs=[pdu],
            )

        return deferreds
