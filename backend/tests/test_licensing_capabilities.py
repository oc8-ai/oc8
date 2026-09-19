from oc8.licensing.capabilities import ENTERPRISE_SSO_SAML, ENTERPRISE_SUPERVISION_ADVANCED


def test_enterprise_sso_saml_capability_is_distinct_from_supervision() -> None:
    assert ENTERPRISE_SSO_SAML == "enterprise.sso.saml"
    assert ENTERPRISE_SSO_SAML != ENTERPRISE_SUPERVISION_ADVANCED
