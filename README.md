# AI-Shopping-Support-Agent-

# AI-powered shopping and customer support agent** for a WordPress WooCommerce consumer electronics store, built using **Microsoft Foundry agent patterns**.

This project demonstrates how to design, ground, and deploy a **production-style AI agent** that helps customers discover products, compare electronics, answer pre-purchase questions, and handle post-purchase support — all while following responsible AI and cost-aware design principles.

---

## 🎯 Problem Statement

Consumer electronics e-commerce stores face three recurring challenges:

1. Customers struggle to find the *right* product among many similar specifications.
2. Customers want to know about specific product specification with having to read long information about the product.
3. Pre-purchase questions overload support teams
4. Post-purchase troubleshooting and order queries are repetitive and costly

# Recap of Your 3 Recurring Challenges

# Product Discovery & Recommendation # – helping customers find the right electronics

## Pre-Purchase Questions / Q&A # – answering specific product or policy questions

# Post-Purchase Support # – troubleshooting, order inquiries, returns, warranty

This project solves these problems by introducing a **Foundry-based AI agent** that:

* Understands customer intent
* Grounds responses in real WooCommerce data and policies
* Automates high-volume support scenarios
* Escalates safely when needed

---

## 🧠 Agent Design (Perception – Reasoning – Action)

### 1️⃣ Perception

The agent collects and structures signals from:

* User messages (intent, entities, preferences)
* Conversation state
* WooCommerce product descriptions,specifications, order, and inventory data
* Store policies (returns, warranty, delivery)

### 2️⃣ Reasoning

Using Microsoft Foundry agent logic, the agent:

* Classifies user intent (browse, compare, support, order inquiry)
* Selects the correct tools (product search, order lookup, policy retrieval)
* Applies guardrails and grounding checks
* Generates contextual, explainable responses

### 3️⃣ Action

The agent executes:

* WooCommerce REST API calls
* Product recommendation ranking
* Troubleshooting flows
* Secure order-status lookups
* Human escalation when confidence is low

---

## 🤖 Core Capabilities

### 🛒 Product Discovery & Recommendations

* Natural language product search
* Budget, brand, and use-case filtering
* Ranked recommendations with explanations

Example:

> "I need a noise-cancelling headset under £200 for remote work"

---

### ⚖️ Product Comparison

* Side-by-side comparison of electronics
* Feature-based pros and cons
* Recommendations tailored to user intent (gaming, photography, business)

---

### ❓ Pre-Purchase Q&A (Grounded)

* Answers sourced strictly from product specs and documentation
* Explicit handling of unknowns (no hallucinations)
* Confidence-aware responses

---

### 🛠️ Post-Purchase Support & Troubleshooting

* Step-by-step troubleshooting guides
* Decision-tree logic
* Support ticket escalation (mocked)

Example:

> "My Bluetooth headphones won’t connect"

---

### 📦 Order & Policy Assistant

* Order status lookups (secure / mocked authentication)
* Returns and warranty policy explanations
* Delivery and refund guidance

---

## 🏗️ Architecture Overview

```
User
 ↓
WordPress Chat UI
 ↓
AI Agent (Microsoft Foundry)
 ├─ Perception Layer
 ├─ Reasoning Layer
 └─ Action Layer
 ↓
WooCommerce REST API | Policy Knowledge Base
```

---

## 🛠️ Tech Stack

| Layer     | Technology                   |
| --------- | ---------------------------- |
| CMS       | WordPress + WooCommerce      |
| AI Agent  | Microsoft Foundry            |
| LLM       | Azure OpenAI                 |
| Retrieval | Azure AI Search / Foundry KB |
| Backend   | Python / Azure Functions     |
| Frontend  | WordPress Chat Widget        |
| APIs      | WooCommerce REST API         |

---

## 📁 Repository Structure

```
ai-woocommerce-agent/
├── README.md
├── architecture/
│   ├── agent-flow-diagram.png
│   └── foundry-mapping.md
├── agent/
│   ├── perception.py
│   ├── reasoning.py
│   ├── action.py
│   └── guardrails.py
├── integrations/
│   ├── woocommerce_client.py
│   └── policy_retriever.py
├── data/
│   └── sample_products.json
├── demos/
│   └── conversation_examples.md
└── deployment/
    └── azure-functions.md
```

---

## 🔒 Responsible AI & Safety

This project explicitly demonstrates:

* Grounded responses (no free-form hallucinations)
* Clear separation of perception, reasoning, and action
* Confidence thresholds and fallback responses
* Human escalation paths
* Cost-aware prompt and token design

---

## 🚀 Running the Project (Local)

1. Clone the repository
2. Configure WooCommerce API credentials
3. Set Azure OpenAI and Foundry environment variables
4. Run the Python agent locally or deploy via Azure Functions

Detailed steps are available in `/deployment/azure-functions.md`.

---

## 🎥 Demo

Screenshots, sample conversations, and architecture diagrams are available in the `/demos` folder.

---

## 📌 Future Enhancements

* Multilingual support
* Cart abandonment recovery agent
* Personalised loyalty recommendations
* Agent analytics dashboard
* Copilot Studio vs Foundry comparison

---

## 📄 License

MIT License

---

**Author:** Philip Ibekwe
**Role Focus:** GenAI Automation | AI Agents | Microsoft Ecosystem

