import asyncio
import re
import unicodedata
from copy import copy
from io import BytesIO
from typing import Literal
from urllib.parse import quote_plus

import cbor2
import cwt
import qrcode
from base45 import b45encode, b45decode
from cwt.cwt import COSEKeyInterface
from httpx import HTTPError
from pydantic import BaseModel
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.colormasks import SolidFillColorMask

import app.routes.tag as tag
from app.errors import CannotSignInvalidIssuer, TagsError
from app.log import logger
from app.utils import fetch_json_file
from settings import conf
from testdata import INVALID_KEY_DATA, DUMMY_JWK

from app.dataproduct import get_dataspace_configuration

# COSE algorithms from technical format to string argment format
# https://python-cwt.readthedocs.io/en/stable/algorithms.html#cose-algorithms
ALGS = {
    -257: "RS256"
}

RSA_PRIVATE_KEY = cwt.COSEKey.from_pem(key_data=conf.RSA_PRIVATE_KEY, alg="RS256", kid=conf.RSA_KID)
INVALID_PRIVATE_KEY = cwt.COSEKey.from_pem(key_data=INVALID_KEY_DATA, alg="RS256", kid=conf.RSA_KID)

# Version prefix for all tags
IOXIO_TAGS_HEADER = "IT1:".encode("utf-8")

CORRECTIONS = {
    "L": qrcode.ERROR_CORRECT_L,
    "M": qrcode.ERROR_CORRECT_M,
    "Q": qrcode.ERROR_CORRECT_Q,
    "H": qrcode.ERROR_CORRECT_H,
}


class CodePayload(BaseModel):
    iss: str
    product: str
    id: str


class CodeBasics(BaseModel):
    payload: CodePayload
    alg: str
    kid: str


class ProductPassport(BaseModel):
    jwks_uri: str
    logo_url: str
    product_dataspace: str


class ProductMetadata(BaseModel):
    names: dict[str, str]
    image_url: str
    supported_dataproducts: list[dict]


def get_product_passport_uri(iss: str):
    return f"https://{iss}/.well-known/product-passport.json"


def get_product_metadata_uri(iss: str, product: str):
    return f"https://{iss}/.well-known/product-passport/products/{product}.json"


def ioxio_tag_str_to_cose_bytes(code: str) -> bytes:
    """
    IOXIO Tag strings start with `IT1:` which is followed by Base45 encoded COSE bytes.
    :param str code: Original tag string
    :return bytes: Base45 decoded COSE bytes
    """
    if code.startswith("IT1:"):
        code_b45 = code[4:]
    else:
        raise TagsError(
            error="Not an IOXIO Tag code, missing version identifier.",
            code="signature_verification_failed",
        )

    try:
        return b45decode(code_b45)
    except ValueError:
        raise TagsError(
            error="Not an IOXIO Tag code, failed Base45 decode",
            code="signature_verification_failed",
        )


def cose_parse_insecure(cose_bytes: bytes) -> CodeBasics:
    """
    Parse the important details from the COSE bytes that are required for further processing. Do NOT verify anything.
    """
    msg = cwt.COSEMessage.loads(cose_bytes)

    # Extract alg and Key ID
    alg = ALGS[msg.protected[1]]
    kid = msg.unprotected[4]

    # Pretend like we have a key for this code
    dummy_jwk = copy(DUMMY_JWK)
    dummy_jwk["kid"] = kid
    dummy_jwk["alg"] = alg

    dummy_key = cwt.COSEKey.from_jwk(dummy_jwk)
    # Override "verify" to always say it is ok
    dummy_key.verify = lambda x, y: True

    try:
        payload = cwt.decode(cose_bytes, dummy_key, no_verify=True)
        return CodeBasics(
            alg=alg,
            kid=kid,
            payload=CodePayload(**payload)
        )
    except cwt.CWTError as e:
        raise TagsError(
            error=str(e),
            code="signature_verification_failed",
        )


def cose_verify(cose_bytes: bytes, key: COSEKeyInterface):
    """
    Verify COSE signature, raises exceptions if it fails.
    """
    cwt.decode(cose_bytes, key)


async def verify_code(code_b45: str):
    cose_bytes = ioxio_tag_str_to_cose_bytes(code_b45)
    basics = cose_parse_insecure(cose_bytes)

    try:
        product_passport = ProductPassport(**await fetch_json_file(get_product_passport_uri(basics.payload.iss)))
    except HTTPError:
        raise TagsError(
            error="Signature verification failed, couldn't read metadata from domain.",
            code="invalid_issuer_cannot_read_product_passport_metadata",
        )

    try:
        jwks = await fetch_json_file(product_passport.jwks_uri)
    except HTTPError:
        raise TagsError(
            error="Signature verification failed, couldn't read JWKS keys from domain.",
            code="invalid_issuer_cannot_read_jwks",
        )

    try:
        # Find the correct JWK
        jwk = next(jwk for jwk in jwks["keys"] if jwk["kid"] == basics.kid and jwk["alg"] == basics.alg)
        cose_key = cwt.COSEKey.from_jwk(jwk)

        # Decode and verify
        cose_verify(cose_bytes, cose_key)
    except StopIteration:
        raise TagsError(
            error="Signature verification failed, matching key was not found.",
            code="invalid_signature_jwks_invalid_key",
        )
    except cwt.CWTError:
        raise TagsError(
            error="Signature verification failed.",
            code="invalid_signature_jwks_failed",
        )


async def fetch_metadata(iss: str, product: str):
    product_passport_uri = get_product_passport_uri(iss)
    product_uri = get_product_metadata_uri(iss, product)

    try:
        product_passport, product_metadata = await asyncio.gather(
            fetch_json_file(product_passport_uri),
            fetch_json_file(product_uri),
        )

        product_passport = ProductPassport(**product_passport)
        product_metadata = ProductMetadata(**product_metadata)
    except HTTPError:
        logger.exception(f"Failed to fetch metadata for {iss}")
        raise TagsError(
            error="Failed to fetch metadata, or validation failed.",
            code="failed_to_fetch_metadata",
        )

    try:
        config = await get_dataspace_configuration(product_passport.product_dataspace)
        gateway = config["product_gateway_url"]
    except KeyError:
        logger.exception(
            f"product_gateway_url not found in {product_passport.product_dataspace} configuration")
        raise TagsError(
            error="Product gateway URL not found in dataspace configuration.",
            code="failed_to_fetch_metadata",
        )

    pgw_openapi = await fetch_json_file(f"{gateway}/openapi.json")

    sdp_definitions = pgw_openapi.get("paths", {})

    return tag.MetadataV1Response(
        logo_url=product_passport.logo_url,
        product_dataspace=product_passport.product_dataspace,
        names=product_metadata.names,
        image_url=product_metadata.image_url,
        supported_dataproducts=[
            {
                "name": found_sdp['post']['summary'],
                "description": found_sdp['post']['description'],
                **sdp
            }
            for sdp in product_metadata.supported_dataproducts if (found_sdp := sdp_definitions.get(sdp['path']))
        ],
    )


def slugify(value: str, allow_unicode=False) -> str:
    """
    Based on https://github.com/django/django/blob/master/django/utils/text.py
    Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
    dashes to single dashes. Remove characters that aren't alphanumerics,
    underscores, dots, or hyphens. Convert to lowercase. Also strip leading and
    trailing whitespace, dashes, and underscores.
    """
    value = str(value)
    if allow_unicode:
        value = unicodedata.normalize('NFKC', value)
    else:
        value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^.\w\s-]', '', value.lower())
    return re.sub(r'[-\s]+', '-', value).strip('-_')


def make_image_filename(iss: str, product: str, id: str, security: str) -> str:
    return f"{slugify(iss)}_{slugify(product)}_{slugify(id)}_{slugify(security)}.png"


def make_image(payload: bytes, frame_type: Literal["simple", "secure"]) -> bytes:
    qr = qrcode.QRCode(error_correction=CORRECTIONS[conf.QR_CORRECTION_LEVEL])
    qr.add_data(payload)

    img = qr.make_image(
        color_mask=SolidFillColorMask(
            front_color=conf.QR_FOREGROUND,
            back_color=conf.QR_BACKGROUND,
        ),
        image_factory=StyledPilImage,
    )

    # TODO: Wrap in frame using cairosvg + pillow

    # Get image contents as bytes
    container = BytesIO()
    img.save(container)
    return container.getvalue()


def make_cose_code(iss: str, product: str, id: str, valid: bool) -> (str, bytes):
    if valid:
        if iss != conf.RSA_ISS:
            raise CannotSignInvalidIssuer()
        private_key = RSA_PRIVATE_KEY
    else:
        private_key = INVALID_PRIVATE_KEY

    if valid:
        logger.info(f"Making a COSE signed IOXIO Tag for iss {iss}, product {product}, id {id}, with a valid signature")
    else:
        logger.info(
            f"Making a COSE signed IOXIO Tag for iss {iss}, product {product}, id {id}, with an invalid signature")

    raw_data = {
        "iss": iss,
        "product": product,
        "id": id,
    }

    cbor_data = cbor2.dumps(raw_data)
    cose = cwt.COSE(alg_auto_inclusion=True, kid_auto_inclusion=True)
    cose_encoded = cose.encode(cbor_data, private_key)
    payload = IOXIO_TAGS_HEADER + b45encode(cose_encoded)

    if valid:
        filename = make_image_filename(iss, product, id, "signed")
    else:
        filename = make_image_filename(iss, product, id, "invalid")

    return filename, make_image(payload, "secure")


def make_url_code(iss: str, product: str, id: str) -> (str, bytes):
    logger.info(f"Making a simple IOXIO Tag for iss {iss}, product {product}, id {id}, in URL format")

    url = "https://tags.ioxio.dev/q/"
    url += "/".join([
        quote_plus(iss),
        quote_plus(product),
        quote_plus(id),
    ])

    filename = make_image_filename(iss, product, id, "simple")
    return filename, make_image(url.encode("utf-8"), "simple")
