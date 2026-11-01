// Call a self-hosted RouteLLM gateway over HTTPS from Node/serverless.
// Usage:
//   ROUTELLM_BASE_URL=https://routellm.example.com ROUTELLM_API_KEY=rk_... node call_routellm.mjs "Hello"
const baseUrl = process.env.ROUTELLM_BASE_URL ?? "https://routellm.example.com";
const apiKey = process.env.ROUTELLM_API_KEY;
if (!apiKey) {
  console.error("Set ROUTELLM_API_KEY to your rk_ key");
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
  console.error(`RouteLLM HTTP ${response.status}: ${await response.text()}`);
  process.exit(1);
}

const data = await response.json();
console.log(JSON.stringify(data, null, 2));
