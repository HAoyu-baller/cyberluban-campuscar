# Security Notes

- Do not commit NTRIP usernames/passwords, NUC passwords, control tokens,
  private keys, certificates, or complete authentication logs.
- Put runtime secrets in `/etc/cyberluban-control.env` on the NUC with mode
  `0600`, or in a local untracked `.env` file.
- Rotate any credential that has ever been pasted into shell history or a
  shared document.
- Keep rosbridge and the NUC control web server on the trusted campus LAN or
  behind an authenticated reverse proxy. Do not expose them to the public
  Internet.
- The lawn model is a baseline perception component. Its output must not be
  treated as a standalone authorization to spray.
