// sudo's askpass helper for the agent's commands (askpass.ts): asks the extension, prints the password for sudo.
// Runs on its own under VS Code's runtime (ELECTRON_RUN_AS_NODE), so it imports nothing but Node.
import * as http from 'http';

const req = http.request(
  {
    socketPath: process.env.DLNE_ASKPASS_HANDLE,
    method: 'POST',
    path: '/',
    headers: { 'x-dlne-askpass': process.env.DLNE_ASKPASS_ID ?? '' },
  },
  (res) => {
    let body = '';
    res.setEncoding('utf8');
    res.on('data', (chunk) => { body += chunk; });
    res.on('end', () => {
      if (res.statusCode !== 200) process.exit(1);
      process.stdout.write(body + '\n');
    });
  }
);
req.on('error', () => process.exit(1));
req.end(process.argv[2] ?? '');
