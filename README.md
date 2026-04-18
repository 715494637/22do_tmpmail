# 22do_tmpmail

Pure-request `22.do` temporary mailbox client for:

- generating or reusing a mailbox
- polling inbox HTML for new messages
- parsing message detail pages
- downloading raw `.eml` content

The project keeps the stable business chain only:

`gmail -> login -> inbox html -> content html -> download eml`

It does not rely on CDP or browser automation for the delivered workflow.

## Stack

- Python 3
- `curl_cffi`

## Files

- `main.py`: request scheduler, inbox polling, message fetch, HTML parsing, `.eml` parsing
- `reverse-records/请求链路.md`: reverse notes and validated request chain

## Install

```bash
pip install curl_cffi
```

## Usage

Fetch the latest message from an existing mailbox:

```bash
python .\main.py --email st.odezgdvkp@googlemail.com --limit 1
```

Create a random mailbox and fetch messages:

```bash
python .\main.py --limit 1
```

Poll inbox HTML until a new matching email appears:

```bash
python .\main.py --email st.odezgdvkp@googlemail.com --wait-mail --limit 1 --match-subject verify --wait-timeout 180 --poll-interval 5
```

Poll using a fixed lower-bound timestamp:

```bash
python .\main.py --email st.odezgdvkp@googlemail.com --wait-mail --limit 1 --since-timestamp 1774017076 --match-from shein
```

## Notes

- The delivered flow uses inbox HTML polling instead of `/action/mailbox/message`.
- `curl_cffi` browser impersonation is required to keep the request chain stable.
- Output is printed as structured JSON.
