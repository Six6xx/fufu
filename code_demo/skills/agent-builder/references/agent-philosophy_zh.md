# The Philosophy of Agent Framework Engineering

> **The model already knows how to be an agent. Your job is to build it a world worth acting in.**

## Foundational Truths

Strip away all frameworks, all libraries, all architectural patterns. What remains?

A loop. A model. An invitation to act.

The agent is not the code. The agent *is* the model itself—the vast neural network trained on humanity's collective data of problem-solving, reasoning, and tool use. The code merely provides the model with an opportunity to express its agency.

The code is the **harness**; the model is the **agent**. They are not interchangeable. Confuse them, and you will build the wrong thing.

## What an Agent Is

An agent is a neural network—a Transformer, an RNN, or some other learned function—trained through billions of gradient updates on sequences of actions to perceive its environment, reason about goals, and take actions to achieve them.

Humans are agents: biological neural networks shaped by evolution. DeepMind's DQN is an agent: a convolutional network that learned to play Atari games from raw pixels. OpenAI Five is an agent: five networks that learned Dota 2 teamwork through self-play. Claude is an agent: a language model that learned to reason and act from the breadth of human knowledge.

In all these cases, the agent is the trained model, not the game engine, the Dota 2 client, or the terminal. The agent *is* the model.

## What an Agent Is Not

Prompt plumbing is not an agent. Stringing together LLM API calls with if-else branches, node graphs, or hard-coded routing logic does not create true agency. It creates brittle pipelines—Rube Goldberg machines with an LLM stuffed into a text-completion node.

You cannot engineer agency into existence. Agency is learned, not programmed. No amount of glue code can conjure emergent autonomy. Such systems are merely modern incarnations of Good Old-Fashioned AI (GOFAI)—symbolic rule systems abandoned by the field decades ago, now merely spray-painted with an LLM facade.

## The Harness: What We Actually Build

If the model is the agent, then what is the code? It is the **harness**—the environment that enables the agent to perceive and act within its specific domain.

```
Harness = Tools + Knowledge + Observations + Action Interfaces + Permissions
```

### Tools: The Agent's Hands

Tools answer: **What can the agent *do*?**

Each tool is an atomic action the agent can perform in its environment: read/write files, execute shell commands, make API calls, control a browser, query a database, etc. The model needs to understand what each tool *does*, but it doesn't need to be pre-programmed on *how* to orchestrate them—it will figure that out itself.

**Design Principle**: Atomic, composable, clearly described. Start with 3–5 tools. Only add more when the model consistently fails at tasks due to a missing capability.

### Knowledge: The Agent's Expertise

Knowledge answers: **What does the agent *know*?**

The specialized information needed to turn a general-purpose agent into a domain expert: product documentation, architectural decisions, regulatory requirements, style guides, etc. This should be injected on-demand via `tool_result`, not crammed into the system prompt upfront. Progressive disclosure preserves context space for what truly matters.

**Design Principle**: Available, but not forced. The agent should know what knowledge exists and pull it as needed.

### Context: The Agent's Memory

Context is the thread that weaves individual actions into coherent behavior: what was said, what was tried, what was learned, what was decided.

**Design Principle**: Context is precious and must be protected. Isolate noisy subtasks (s04), compress history when it grows too long (s06), and allow goals to persist beyond a single conversation (s07).

### Permissions: The Agent's Boundaries

Permissions answer: **What is the agent *allowed* to do?**

Sandboxed file access, approval gates for destructive operations, trust boundaries between the agent and external systems—this is where security engineering meets harness engineering.

**Design Principle**: Constrain behavior, not capability. "Only one task can be `in_progress` at a time" enforces sequential focus; a "read-only subagent" prevents accidental modifications.

### Task-Process Data: The Agent's Training Signal

Every sequence of actions your agent performs within your harness is a training signal. The perception-reasoning-action trajectories generated in real-world deployment are the raw material for fine-tuning the next generation of agent models. Your harness doesn't just serve the agent—it helps it evolve.

## The Universal Loop

Regardless of the domain, every effective agent follows the same pattern:

```
Loop:
  Model sees: Conversation History + Available Tools
  Model decides: Perform an Action OR Return a Response
  If Action: Tool is called, result is added to context, loop continues
  If Response: Answer is output, loop ends
```

This is not a simplification; it is the true architecture. Everything else is harness engineering—mechanisms layered on top of this loop to make the agent more effective. The loop belongs to the agent; the mechanisms belong to the harness.

## Principles of Harness Engineering

### Trust the Model

The most important principle: **Trust the model**.

Don't try to anticipate every edge case. Don't build complex decision trees. Don't pre-specify workflows.

The model's reasoning capabilities far exceed any rule system you could write. Your conditional logic will fail on the edges; the model will reason its way through them.

**Give the model tools and knowledge, and let it decide how to use them.**

### Constraints Empower

It seems paradoxical, but constraints don't limit the agent—they focus it.

A todo-list with "only one task can be in progress" enforces sequential focus. A "read-only subagent" prevents accidental writes. A context compression threshold prevents history from drowning the current task.

The best constraints keep the model from getting lost, not from micro-managing its methods.

### Progressive Complexity

Never build everything at once.

```
Layer 0: Model + One Tool (bash)                -- s01
Layer 1: Model + Tool Routing Map               -- s02
Layer 2: Model + Planning                       -- s03
Layer 3: Model + Subagents + Skills             -- s04, s05
Layer 4: Model + Context Management + Persistence -- s06, s07, s08
Layer 5: Model + Teams + Autonomy + Isolation   -- s09-s12
```

Start at the lowest layer that could possibly work. Only evolve upward when real-world usage exposes a need.

## The Mindset Shift

Building harnesses requires a fundamental shift in thinking:

**From**: "How do I make the system do X?"
**To**: "How do I give the model the ability to do X?"

**From**: "What happens when the user says Y?"
**To**: "What tools would help in responding to Y?"

**From**: "What is the workflow for this task?"
**To**: "What does the model need to derive its own workflow?"

**From**: "I am building an agent."
**To**: "I am building a harness for an agent."

The best harness code is almost boring: simple loops, clear tool definitions, clean context management. The magic isn't in the code—it's in the model.

## The Vehicle Metaphor

The model is the driver; the harness is the vehicle.

The coding agent's vehicle is its IDE, terminal, and filesystem. The agricultural agent's vehicle is its sensor array, irrigation controls, and weather data. The hotel agent's vehicle is its booking system, customer communication channels, and facility APIs.

The driver has generalized intelligence; the vehicle is specialized for its domain. As a harness engineer, your job is to build the best possible vehicle for your domain—giving the driver maximum visibility, precise control, and clear boundaries.

Build the cockpit, the dashboard, and the controls. The pilot is already trained.

## Conclusion

The model is the agent; the code is the harness. Be clear about which one you're building.

You are not writing intelligence; you are building the world in which intelligence resides. The quality of that world—the clarity of the agent's perceptions, the precision of its actions, the richness of its knowledge—directly determines how effectively that intelligence can manifest.

Build an excellent harness, and let the agent do the rest.