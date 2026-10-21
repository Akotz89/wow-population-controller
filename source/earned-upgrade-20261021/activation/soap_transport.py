
import base64


import shlex


import urllib.error

import urllib.request

import xml.etree.ElementTree as ET

from pathlib import Path

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

def soap_command(command, credentials, url="http://127.0.0.1:7878/", timeout=20):
    envelope = ET.Element("{" + SOAP_NS + "}Envelope")
    body = ET.SubElement(envelope, "{" + SOAP_NS + "}Body")
    action = ET.SubElement(body, "{urn:AC}executeCommand")
    ET.SubElement(action, "command").text = command
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "urn:AC#executeCommand"}
    if credentials is not None:
        token = base64.b64encode((credentials[0] + ":" + credentials[1]).encode()).decode()
        headers["Authorization"] = "Basic " + token
    request = urllib.request.Request(
        url,
        ET.tostring(envelope, encoding="utf-8", xml_declaration=True),
        headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, payload = error.code, error.read()
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        return {"http_status": status, "ok": False, "xml_parse_error": str(error), "response": payload.decode(errors="replace")[:2048]}
    fault = next((node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Fault"), None)
    result = next((node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "result"), None)
    if result is None:
        result = next((node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "return"), None)
    message = "".join((fault if fault is not None else result if result is not None else root).itertext()).strip()
    return {"http_status": status, "ok": status == 200 and fault is None, "response": message}

def read_credentials(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        if key.strip() in ("ACCOUNT_USERNAME", "ACCOUNT_PASSWORD"):
            words = shlex.split(value, comments=True)
            if len(words) != 1:
                raise ValueError("Invalid private credential field")
            values[key.strip()] = words[0]
    return values["ACCOUNT_USERNAME"], values["ACCOUNT_PASSWORD"]
