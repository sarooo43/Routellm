// Call a self-hosted Routtener gateway over HTTPS from Node/serverless.
// Usage:
//   ROUTTENER_BASE_URL=https://routtener.example.com ROUTTENER_API_KEY=rk_... node call_routellm.mjs "Hello"
const baseUrl = process.env.ROUTTENER_BASE_URL ?? process.env.ROUTELLM_BASE_URL ?? "https://routtener.example.com";
const apiKey = process.env.ROUTTENER_API_KEY ?? process.env.ROUTELLM_API_KEY;
if (!apiKey) {
  console.error("Set ROUTTENER_API_KEY (or legacy ROUTELLM_API_KEY) to your rk_ key");
  process.exit(1);
}

const prompt = process.argv[2] ?? "Hello from a webhook";
const response = await fetch(`${baseUrl.replace(/\/$/, "")}/api/v1/route`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": apiKey,
  },
  body: JSON.stringify({ prompt }),
});

if (!response.ok) {
  console.error(`Routtener HTTP ${response.status}: ${await response.text()}`);
  process.exit(1);
}

const data = await response.json();
console.log(JSON.stringify(data, null, 2));
