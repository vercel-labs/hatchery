# factory

an agent that runs on the cloud, mostly unattended. connected to slack and
github; has its own ui. work is grouped into projects — each with attached
repos, chats, and one memory file capturing current state and direction.

- ping the bot on slack or github and the thread becomes a chat in the ui;
  replies fan out to every surface at once.
- turns run as durable workflows (vercel workflows + ai sdk for python).
- the first factory task: daily e2e-test parity scan between the js and
  python vercel sdks, reported into a chat.

see AGENTS.md for layout and dev commands.
