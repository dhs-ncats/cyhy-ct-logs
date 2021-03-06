#!/usr/bin/env pytest -vs

"""Tests for Certificate tasks."""

import pprint

import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from admiral.certs.tasks import summary_by_domain, cert_by_id

PP = pprint.PrettyPrinter(indent=4)


@pytest.fixture(scope="module")
def celery():
    """Celery app test fixture."""
    from admiral.celery import celery
    return celery


class TestCerts:
    """Test certificate transparency tasks."""

    # @pytest.mark.filterwarnings("ignore:'async' and 'await'")
    def test_end_to_end(self, celery):
        """Perform and end-to-end test of the certificate log tasks."""
        summary = summary_by_domain.delay('cyber.dhs.gov')
        assert summary.get(
            timeout=60) is not None, 'Summary result should not be None'
        assert len(summary.get()) > 0, \
            'Summary should return at least one result'
        PP.pprint(summary.get())
        print(f'received {len(summary.get())} summary records')

        # get the first id from the summaries
        id = summary.get()[0]['min_cert_id']
        print(f'requesting certificate for id: {id}')
        first_cert = cert_by_id.delay(id)
        pem = first_cert.get(timeout=60)
        print('done')

        cert = x509.load_pem_x509_certificate(
            bytes(pem, 'utf-8'), default_backend())
        print(f'certificate serial number: {cert.serial_number}')
