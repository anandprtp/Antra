# Antra Player

Mobile-first private music player for the Antra Telegram bot.

The bot issues a personal URL whose fragment contains a bearer session and the
current API origin. The fragment is removed from browser history immediately;
audio is delivered through short-lived signed stream URLs with HTTP Range
support.

## Local development

Requires Node.js 22.13 or newer.

```bash
npm install
npm run dev
```

Verification:

```bash
npm test
npm run lint
```

The production catalog is available only through links issued by the Telegram
bot. A UI-only demo catalog can be enabled explicitly with
`NEXT_PUBLIC_PLAYER_DEMO=true`.
