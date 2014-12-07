"""
:mod:`~asyncio_xmpp.security_layer` --- Implementations to negotiate stream security
####################################################################################

This module provides different implementations of the security layer (TLS+SASL).

These are coupled, as different SASL features might need different TLS features
(such as channel binding or client cert authentication).

.. autofunction:: tls_with_password_based_authentication(password_provider, [ssl_context_factory], [max_auth_attempts=3])

.. autofunction:: security_layer

.. autofunction:: negotiate_stream_security

Partial security providers
==========================

Partial security providers serve as arguments to pass to
:func:`negotiate_stream_security`.

.. _tls providers:

Transport layer security provider
---------------------------------

As an *tls_provider* argument to :class:`SecurityLayer`, instances of the
following classes can be used:

.. autoclass:: STARTTLSProvider

.. _sasl providers:

SASL providers
--------------

As elements of the *sasl_providers* argument to :class:`SecurityLayer`,
instances of the following classes can be used:

.. autoclass:: PasswordSASLProvider

Abstract base classes
=====================

For implementation of custom SASL providers, the following base class can be
used:

.. autoclass:: SASLProvider
   :members:

"""
import abc
import asyncio
import functools
import logging
import ssl

from . import errors, sasl, stream_elements
from .utils import *

logger = logging.getLogger(__name__)

class STARTTLSProvider:
    """
    A TLS provider to negotiate STARTTLS on an existing XML stream. This
    requires that the stream uses
    :class:`.ssl_wrapper.STARTTLSableTransportProtocol` as a transport.

    *ssl_context_factory* must be a callable returning a valid
    :class:`ssl.SSLContext` object. It is called without
    arguments.

    *require_starttls* can be set to :data:`False` to allow stream negotiation
    to continue even if STARTTLS fails before it has been started (the stream is
    fatally broken if the STARTTLS command has been sent but SSL negotiation
    fails afterwards).

    .. warning::

       Certificate validation requires Python 3.4 to work properly!

    .. note::

       Support for DANE has not been implemented yet, as this also requires
       Python 3.4 and the main developer does not have Python 3.4 yet.

    """

    def __init__(self, ssl_context_factory, *,
                 require_starttls=True, **kwargs):
        super().__init__(**kwargs)
        self._ssl_context_factory = ssl_context_factory
        self._required = require_starttls

    def _fail_if_required(self, msg):
        if self._required:
            raise errors.TLSFailure(msg)
        return None

    @asyncio.coroutine
    def execute(self, client_jid, features, xmlstream):
        """
        Perform STARTTLS negotiation. If successful, a ``(tls_transport,
        new_features)`` pair is returned. Otherwise, if STARTTLS failed
        non-fatally and is not required (see constructor arguments),
        :data:`False` is returned.

        The *tls_transport* member of the return value is the
        :class:`asyncio.Transport` created by asyncio for SSL. The second
        element are the new stream features received after STARTTLS
        negotiation.
        """
        E = xmlstream.tx_context.default_ns_builder(namespaces.starttls)

        try:
            feature = features.require_feature(
                "{{{}}}starttls".format(namespaces.starttls)
            )
        except KeyError:
            return self._fail_if_required("STARTTLS not supported by peer")

        if not hasattr(xmlstream.transport, "starttls"):
            return self._fail_if_required("STARTTLS not supported by us")

        node = yield from xmlstream.send_and_wait_for(
            [
                E("starttls")
            ],
            [
                "{{{}}}proceed".format(namespaces.starttls),
                "{{{}}}failure".format(namespaces.starttls),
            ]
        )

        proceed = node.tag.endswith("}proceed")

        if proceed:
            logger.info("engaging STARTTLS")
            try:
                tls_transport, _ = yield from xmlstream.transport.starttls(
                    ssl_context=self._ssl_context_factory(),
                    server_hostname=client_jid.domainpart)
            except Exception as err:
                logger.exception("STARTTLS failed:")
                raise errors.TLSFailure("TLS connection failed: {}".format(err))
            return tls_transport

        return self._fail_if_required("STARTTLS failed on remote side")

class SASLProvider:
    def _find_supported(self, features, mechanism_classes):
        """
        Return a supported SASL mechanism class, by looking the given
        stream features *features*.

        If SASL is not supported at all, :class:`~.errors.SASLFailure` is
        raised. If no matching mechanism is found, ``(None, None)`` is
        returned. Otherwise, a pair consisting of the mechanism class and the
        value returned by the respective
        :meth:`~.sasl.SASLMechanism.any_supported` method is returned.
        """

        try:
            mechanisms = features.require_feature("{{{}}}mechanisms".format(
                namespaces.sasl))
        except KeyError:
            logger.error("No sasl mechanisms: %r", list(features))
            raise errors.SASLUnavailable(
                "Remote side does not support SASL") from None

        remote_mechanism_list = [
            mechanism.text
            for mechanism in mechanisms.iterchildren("{{{}}}mechanism".format(
                    namespaces.sasl))
            if mechanism.text
        ]

        for our_mechanism in mechanism_classes:
            token = our_mechanism.any_supported(remote_mechanism_list)
            if token is not None:
                return our_mechanism, token

        return None, None

    AUTHENTICATION_FAILURES = {
        "credentials-expired",
        "account-disabled",
        "invalid-authzid",
        "not-authorized",
        "temporary-auth-failure",
    }

    MECHANISM_REJECTED_FAILURES = {
        "invalid-mechanism",
        "mechanism-too-weak",
        "encryption-required",
    }

    @asyncio.coroutine
    def _execute(self, xmlstream, mechanism, token):
        """
        Execute SASL negotiation using the given *mechanism* instance and
        *token* on the *xmlstream*.
        """
        sm = sasl.SASLStateMachine(xmlstream)
        try:
            yield from mechanism.authenticate(sm, token)
            return True
        except errors.SASLFailure as err:
            if err.xmpp_error in self.AUTHENTICATION_FAILURES:
                raise errors.AuthenticationFailure(
                    xmpp_error=err.xmpp_error,
                    text=err.text)
            elif err.xmpp_error in self.MECHANISM_REJECTED_FAILURES:
                return False
            raise

    @abc.abstractmethod
    @asyncio.coroutine
    def execute(self,
                client_jid,
                features,
                xmlstream,
                tls_transport):
        """
        Perform SASL negotiation. The implementation depends on the specific
        :class:`SASLProvider` subclass in use.

        This coroutine returns :data:`True` if the negotiation was
        successful. If no common mechanisms could be found, :data:`False` is
        returned. This is useful to chain several SASL providers (e.g. a
        provider supporting ``EXTERNAL`` in front of password-based providers).

        Any other error case, such as no SASL support on the remote side or
        authentication failure results in an :class:`~.errors.SASLFailure`
        exception to be raised.
        """

class PasswordSASLProvider(SASLProvider):
    """
    Perform password-based SASL authentication.

    *jid* must be a :class:`~.jid.JID` object for the
    client. *password_provider* must be a coroutine which is called with the jid
    as first and the number of attempt as second argument. It must return the
    password to use, or :data:`None` to abort. In that case, an
    :class:`errors.AuthenticationFailure` error will be raised.

    At most *max_auth_attempts* will be carried out. If all fail, the
    authentication error of the last attempt is raised.

    The SASL mechanisms used depend on whether TLS has been negotiated
    successfully before. In any case, :class:`~.sasl.SCRAM` is used. If TLS has
    been negotiated, :class:`~.sasl.PLAIN` is also supported.
    """

    def __init__(self, password_provider, *,
                 max_auth_attempts=3, **kwargs):
        super().__init__(**kwargs)
        self._password_provider = password_provider
        self._max_auth_attempts = max_auth_attempts

    @asyncio.coroutine
    def execute(self,
                client_jid,
                features,
                xmlstream,
                tls_transport):
        client_jid = client_jid.bare

        password_signalled_abort = False
        nattempt = 0
        cached_credentials = None

        @asyncio.coroutine
        def credential_provider():
            nonlocal password_signalled_abort, nattempt, cached_credentials
            if cached_credentials is not None:
                return client_jid.localpart, cached_credentials

            password = yield from self._password_provider(
                client_jid, nattempt)
            if password is None:
                password_signalled_abort = True
                raise errors.AuthenticationFailure(
                    "Authentication aborted by user")
            cached_credentials = password
            return client_jid.localpart, password

        classes = [
            sasl.SCRAM
        ]
        if tls_transport is not None:
            classes.append(sasl.PLAIN)

        while classes:
            # go over all mechanisms available. some errors disable a mechanism
            # (like encryption-required or mechansim-too-weak)
            mechanism_class, token = self._find_supported(features, classes)
            if mechanism_class is None:
                return False

            mechanism = mechanism_class(credential_provider)
            last_auth_error = None
            for nattempt in range(self._max_auth_attempts):
                try:
                    mechanism_worked = yield from self._execute(
                        xmlstream, mechanism, token)
                except errors.AuthenticationFailure as err:
                    if password_signalled_abort:
                        # immediately re-raise
                        raise
                    last_auth_error = err
                    # allow the user to re-try
                    cached_credentials = None
                    continue
                else:
                    break
            else:
                raise last_auth_error

            if mechanism_worked:
               return True
            classes.remove(mechanism_class)

        return False

def default_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    ctx.options |= ssl.OP_NO_SSLv2
    ctx.options |= ssl.OP_NO_SSLv3
    return ctx

@asyncio.coroutine
def negotiate_stream_security(tls_provider, sasl_providers,
                              negotiation_timeout, jid, features, xmlstream):
    """
    Negotiate stream security for the given *xmlstream*. For this to work,
    *features* must be the most recent
    :class:`.stream_elements.StreamFeatures` node.

    First, transport layer security is negotiated using *tls_provider*. If that
    fails non-fatally, negotiation continues as normal. Exceptions propagate
    upwards.

    After TLS has been tried, SASL is negotiated, by sequentially attempting
    SASL negotiation using the providers in the *sasl_providers* list. If a
    provider fails to negotiate SASL with an
    :class:`~.errors.AuthenticationFailure` or has no mechanisms in common with
    the peer server, the next provider can continue. Otherwise, the exception
    propagates upwards.

    If no provider succeeds and there was an authentication failure, that error
    is re-raised. Otherwise, a dedicated :class:`~.errors.SASLFailure` exception
    is raised, which states that no common mechanisms were found.

    On success, a pair of ``(tls_transport, features)`` is returned. If TLS has
    been negotiated, *tls_transport* is the SSL :class:`asyncio.Transport`
    created by asyncio (as returned by the *tls_provider*). If no TLS has been
    negotiated, *tls_transport* is :data:`None`. *features* is the latest
    :class:`~.stream_elements.StreamFeatures` element received during
    negotiation.

    On failure, an appropriate exception is raised. Authentication failures
    can be caught as :class:`.errors.AuthenticationFailure`. Errors related
    to SASL or TLS negotiation itself can be caught using
    :class:`~.errors.SASLFailure` and :class:`~.errors.TLSFailure`
    respectively.
    """

    tls_transport = yield from tls_provider.execute(jid, features, xmlstream)

    if tls_transport is not None:
        features = yield from xmlstream.reset_stream_and_get_features(
            timeout=negotiation_timeout)

    last_auth_error = None
    for sasl_provider in sasl_providers:
        try:
            result = yield from sasl_provider.execute(
                jid, features, xmlstream, tls_transport)
        except errors.AuthenticationFailure as err:
            last_auth_error = err
            continue

        if result:
            features = yield from xmlstream.reset_stream_and_get_features(
                timeout=negotiation_timeout)
            break
    else:
        if last_auth_error:
            raise last_auth_error
        else:
            raise errors.SASLUnavailable("No common mechanisms")

    return tls_transport, features

def security_layer(tls_provider, sasl_providers):
    """
    .. seealso::

       Use this function only if you need more customization than provided by
       :func:`tls_with_password_based_authentication`.

    Return a partially applied :func:`negotiate_stream_security` function, where
    the *tls_provider* and *sasl_providers* arguments are already bound.

    The return value can be passed to the constructor of :class:`~.node.Client`.

    Some very basic checking on the input is also performed.
    """

    tls_provider.execute  # check that tls_provider has execute method
    sasl_providers = list(sasl_providers)
    if not sasl_providers:
        raise ValueError("At least one SASL provider must be given.")
    for sasl_provider in sasl_providers:
        sasl_provider.execute  # check that sasl_provider has execute method

    return functools.partial(negotiate_stream_security,
                             tls_provider, sasl_providers)


def tls_with_password_based_authentication(
        password_provider,
        ssl_context_factory=default_ssl_context,
        max_auth_attempts=3):
    """
    Produce a commonly used security layer, which uses TLS and password
    authentication. If *ssl_context_factory* is not provided, an SSL context
    with TLSv1+ is used.

    *password_provider* must be a coroutine which is called with the jid
    as first and the number of attempt as second argument. It must return the
    password to us, or :data:`None` to abort.

    Return a security layer which can be passed to :class:`~.node.Client`.
    """

    return security_layer(
        tls_provider=STARTTLSProvider(ssl_context_factory,
                                      require_starttls=True),
        sasl_providers=[PasswordSASLProvider(
            password_provider,
            max_auth_attempts=max_auth_attempts)]
    )