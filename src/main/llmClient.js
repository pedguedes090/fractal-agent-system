function buildChatUrl(serverUrl) {
  const base = String(serverUrl || "").trim().replace(/\/+$/, "");
  if (!base) throw new Error("Chua co link server model.");
  return `${base}/chat/completions`;
}

function extractText(data) {
  const message = data?.choices?.[0]?.message;
  if (!message) return "";
  if (typeof message.content === "string") return message.content;
  if (Array.isArray(message.content)) {
    return message.content
      .map((part) => {
        if (typeof part === "string") return part;
        return part?.text || "";
      })
      .join("");
  }
  return "";
}

function extractStreamingText(rawText) {
  const lines = String(rawText || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.startsWith("data:"));

  let text = "";
  for (const line of lines) {
    const payload = line.replace(/^data:\s*/, "");
    if (!payload || payload === "[DONE]") continue;
    try {
      const data = JSON.parse(payload);
      const choice = data?.choices?.[0];
      text += choice?.delta?.content || choice?.message?.content || "";
    } catch {
      // Ignore malformed keepalive or proxy lines.
    }
  }
  return text;
}

async function chatCompletion({ serverUrl, model, messages, temperature = 0.2, responseFormat }) {
  const response = await fetch(buildChatUrl(serverUrl), {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      model,
      messages,
      temperature,
      ...(responseFormat ? { response_format: responseFormat } : {})
    })
  });

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`Model server loi ${response.status}: ${body.slice(0, 400)}`);
  }

  const rawText = await response.text();
  try {
    return extractText(JSON.parse(rawText));
  } catch {
    const streamed = extractStreamingText(rawText);
    if (streamed) return streamed;
    throw new Error(`Khong doc duoc phan hoi model: ${rawText.slice(0, 240)}`);
  }
}

function extractJson(text) {
  const raw = String(text || "").trim();
  if (!raw) throw new Error("Model khong tra ve noi dung.");

  try {
    return JSON.parse(raw);
  } catch {
    const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/i);
    if (fenced) return JSON.parse(fenced[1]);

    const firstObject = raw.indexOf("{");
    const lastObject = raw.lastIndexOf("}");
    if (firstObject >= 0 && lastObject > firstObject) {
      return JSON.parse(raw.slice(firstObject, lastObject + 1));
    }

    throw new Error("Khong doc duoc JSON tu model.");
  }
}

async function jsonCompletion(options) {
  const text = await chatCompletion({
    ...options,
    responseFormat: { type: "json_object" }
  });
  return extractJson(text);
}

module.exports = {
  chatCompletion,
  jsonCompletion
};
