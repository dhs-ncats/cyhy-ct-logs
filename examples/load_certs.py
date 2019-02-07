#!/usr/bin/env python3

"""load-certs: A tool to download certificates from CT logs.

This is the sample of a tool to download CT Logs via celery tasks and store
them in a mongo database.
"""
from datetime import datetime
import pprint
import time

from admiral.celery import celery  # noqa: F401, inits on import
from admiral.certs.tasks import summary_by_domain, cert_by_id
from celery import group
from cryptography import x509
from cryptography.hazmat.backends import default_backend
import dateutil.parser as parser
from pymodm import context_managers
from tqdm import tqdm

from models import Cert, Domain
from util import connect_from_config

# Globals
PP = pprint.PrettyPrinter(indent=4)
EARLIEST_EXPIRED_DATE = parser.parse('2018-10-01')


def get_earliest_sct(xcert):
    """Calculate the earliest time this certificate was logged to a CT log.

    If it was not logged by the CA, then the not_before time is returned.

    Arguments:
    xcert -- an x509 certificate object

    Returns (datetime, bool):
        datetime: the earliest calculated date.
        bool: True if an SCT was used, False otherwise
    """
    try:
        earliest = datetime.max
        scts = xcert.extensions.get_extension_for_class(
            x509.PrecertificateSignedCertificateTimestamps).value
        for sct in scts:
            earliest = min(earliest, sct.timestamp)
        return earliest, True
    except x509.extensions.ExtensionNotFound:
        return xcert.not_valid_before, False


def is_poisioned(xcert):
    """Determine if an x509 certificate has a precertificate poision extension.

    Arguments:
    xcert -- an x509 certificate object
    """
    try:
        xcert.extensions.get_extension_for_oid(
            x509.oid.ExtensionOID.PRECERT_POISON)
        return True
    except x509.extensions.ExtensionNotFound:
        return False


def get_sans_set(xcert):
    """Extract the set of subjects from the SAN extension.

    Arguments:
    xcert -- an x509 certificate object

    Returns a set of strings containing the subjects
    """
    try:
        san = xcert.extensions.get_extension_for_oid(
            x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        dns_names = set(san.get_values_for_type(x509.DNSName))
    except x509.extensions.ExtensionNotFound:
        dns_names = set()
    # not all subjects have CNs: https://crt.sh/?id=1009394371
    for cn in xcert.subject.get_attributes_for_oid(
            x509.oid.NameOID.COMMON_NAME):
        # TODO make sure the cn is a correct type
        # what the hell is going on here: https://crt.sh/?id=174654356
        # ensure the cn is in the dns_names
        dns_names.add(cn.value)
    return dns_names


def make_cert_from_pem(pem):
    """Create a Cert model object from a PEM certificate string.

    Arguments:
    pem -- PEM encoded certificate

    Returns (cert, precert):
        cert: an x509 certificate object
        precert: a boolean, True if this is a precertificate, False otherwise
    """
    xcert = x509.load_pem_x509_certificate(
        bytes(pem, 'utf-8'), default_backend())
    dns_names = get_sans_set(xcert)

    sct_or_not_before, sct_exists = get_earliest_sct(xcert)

    cert = Cert()
    cert.serial = hex(xcert.serial_number)[2:]
    cert.issuer = xcert.issuer.rfc4514_string()
    cert.not_before = xcert.not_valid_before
    cert.not_after = xcert.not_valid_after
    cert.sct_or_not_before = sct_or_not_before
    cert.sct_exists = sct_exists
    cert.pem = pem
    cert.subjects = dns_names
    return cert, is_poisioned(xcert)


def cert_id_exists_in_database(log_id):
    """Check if a  ID already exists in the either certificate collection.

    Returns True if the id exists, False otherwise
    """
    c = Cert.objects.raw({'_id': log_id})
    if c.count() > 0:
        return True
    with context_managers.switch_collection(Cert, 'precerts'):
        c = Cert.objects.raw({'_id': log_id})
        if c.count() > 0:
            return True
    return False


def get_new_log_ids(domain, max_expired_date):
    """Generate a sequence of new CT Log IDs.

    Arguments:
    domain -- the domain name to query
    max_expired_date -- a date to filter out expired certificates

    Yields a sequence of new, unique, log IDs.
    """
    tqdm.write(f'requesting certificate list for: {domain}')
    expired = domain != 'nasa.gov'  # NASA is breaking the CT Log
    cert_list = summary_by_domain.delay(
        domain, subdomains=True, expired=expired)
    cert_list = cert_list.get()
    duplicate_log_ids = set()
    for i in tqdm(cert_list, desc='Subjects', unit='entries'):
        log_id = i['min_cert_id']
        cert_expiration_date = parser.parse(i['not_after'])
        tqdm.write(f'id: {log_id}:\tex: {cert_expiration_date}\t'
                   f'{i["name_value"]}...\t', end='')
        if cert_expiration_date < max_expired_date:
            tqdm.write('too old')
            continue
        # check to see if we have this certificate already
        if log_id in duplicate_log_ids or cert_id_exists_in_database(log_id):
            # we already have it, skip
            duplicate_log_ids.add(log_id)
            tqdm.write('duplicate')
            continue
        else:
            duplicate_log_ids.add(log_id)
            tqdm.write('will import')
            yield(log_id)


def group_update_domain(domain, max_expired_date):
    """Create parallel tasks to download all new certificates with date filter.

    Arguments:
    domain -- domain name to query
    max_expired_date -- a date to filter out expired certificates

    Returns the number of certificates imported.
    """
    signatures = []
    for log_id in get_new_log_ids(domain.domain, max_expired_date):
        signatures.append(cert_by_id.s(log_id))
    with tqdm(total=len(signatures), desc='Certs', unit='certs') as pbar:
        job = group(signatures)
        results = job.apply_async()
        while not results.ready():
            pbar.update(results.completed_count() - pbar.n)
            time.sleep(0.5)

    tasks_to_results = zip(job.tasks, results.join())
    for task, pem in tasks_to_results:
        cert, is_precert = make_cert_from_pem(pem)
        cert.log_id = task.get('args')[0]  # get log_id from task
        if is_precert:
            # if this is a precert, we save to the precert collection
            with context_managers.switch_collection(Cert, 'precerts'):
                cert.save()
        else:
            cert.save()
    return len(job.tasks)


def main():
    """Start of program."""
    connect_from_config()
    with context_managers.switch_connection(Domain, 'production'):
        query_set = Domain.objects.all()
        print(f'{query_set.count()} domains to process')
        # TODO set batch_size lower, cursor is timing out
        domains = list(query_set.all())
        c = 0
        skip_to = 0
        total_new_count = 0
        for domain in tqdm(domains, desc='Domains', unit='domain'):
            c += 1
            if c < skip_to:
                continue
            tqdm.write('-' * 80)
            tqdm.write(f'domain #{c}')
            new_count = group_update_domain(domain, EARLIEST_EXPIRED_DATE)
            total_new_count += new_count
            tqdm.write(
                f'{new_count} certificates were imported for {domain.domain}')
        print(f'{total_new_count} certificates were imported for'
              f'{len(domains)} domains.')

        import IPython; IPython.embed()  # noqa: E702 <<< BREAKPOINT >>>


if __name__ == '__main__':
    main()
