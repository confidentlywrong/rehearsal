# Confidently Wrong: The Rehearsal — Frontend (`web/frontend`)

This directory contains the Angular 21 Virtual Day-in-the-Life (VDIL) web interface for **Confidently Wrong: The Rehearsal** ([github.com/confidentlywrong/rehearsal](https://github.com/confidentlywrong/rehearsal)).

## Development Workflow

### Prerequisites

- **Node.js** 22+ and **npm**
- Local backend services initialized via `make init` from the repository root

### Commands

```bash
# Install dependencies
npm install

# Run the local development server on port 9119
npm start

# Build the production bundle
npm run build

# Run unit tests
npm test
```

### License Headers

All TypeScript, JavaScript, HTML, and CSS source files must carry the Apache 2.0 license header (`Copyright 2026 Christian Haeussinger`, plus `Copyright 2026 Google LLC` on modified files inherited from upstream [`GoogleCloudPlatform/race-condition`](https://github.com/GoogleCloudPlatform/race-condition)). Run `make check-license` from the repository root to verify.
