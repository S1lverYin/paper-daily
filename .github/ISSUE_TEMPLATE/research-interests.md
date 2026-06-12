---
name: Research Interests
about: Configure paper search topics
title: Research Interests
labels: config
assignees: ''
---

Edit the JSON below. Keep the issue title as `Research Interests`.

```json
{
  "sources": [
    { "type": "arxiv", "name": "arXiv" }
  ],
  "arxiv_categories_whitelist": ["cs.CL", "cs.LG", "cs.AI"],
  "negative_terms": {},
  "topics": [
    {
      "id": "llm_inference",
      "name": "LLM Inference",
      "description": "Efficient serving, KV cache management, scheduling, quantization, and distributed inference.",
      "keywords": [
        "LLM inference",
        "KV cache",
        "inference serving",
        "tensor parallelism",
        "low-bit quantization"
      ],
      "arxiv_categories": ["cs.CL", "cs.LG", "cs.DC"]
    }
  ]
}
```
