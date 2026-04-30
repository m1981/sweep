Here are the architectural and sequence diagrams for both the Backend and Frontend, based on the code provided in our chat. I have used Mermaid.js to visualize the systems.

---

### 1. Backend Architecture & Sequence

The backend is a FastAPI application that orchestrates a highly complex, agentic RAG (Retrieval-Augmented Generation) pipeline. It features a hybrid search engine, graph-based context pruning, and a streaming chat engine that intercepts tool calls mid-stream.

#### Backend Architecture Diagram

```mermaid
graph TD
    subgraph FastAPI Application
        API_Chat["/backend/chat"]
        API_Search["/backend/search"]
        API_Autofix["/backend/autofix"]
        API_PR["/backend/create_pull"]
    end

    subgraph Chat & Agent Engine
        ChatCore["ChatGPT Core (chat.py)"]
        SearchAgent["Search Agent (search_agent.py)"]
        ToolHandler["Tool Orchestrator (api.py)"]
    end

    subgraph Code Discovery & Search
        TicketUtils["Hybrid Search (ticket_utils.py)"]
        Lexical["Tantivy Lexical Index (lexical_search.py)"]
        Vector["Vector DB / Embeddings (vector_db.py)"]
        Reranker["Cohere/Voyage Reranker"]
        AST["Tree-sitter AST Chunking (code_validators.py)"]
    end

    subgraph Advanced Context
        GraphRAG["PageRank GraphRAG (context_pruning.py)"]
        Ripgrep["Ripgrep + Vector Hybrid (dynamic_context_bot.py)"]
    end

    subgraph External Services
        LLM["Anthropic / OpenAI APIs"]
        GitHub["GitHub API / Git Clone"]
        Redis["Redis / DiskCache"]
    end

    %% Connections
    API_Chat --> ChatCore
    API_Search --> TicketUtils
    API_Autofix --> SearchAgent

    ChatCore --> LLM
    ChatCore --> ToolHandler
    ToolHandler --> TicketUtils
    ToolHandler --> Ripgrep

    TicketUtils --> AST
    TicketUtils --> Lexical
    TicketUtils --> Vector
    TicketUtils --> Reranker

    SearchAgent --> GraphRAG
    SearchAgent --> TicketUtils

    Lexical --> Redis
    Vector --> Redis
    AST --> GitHub
```

#### Backend Sequence Diagram: Streaming Chat with Mid-Stream Tool Calling
This diagram details the `/backend/chat` endpoint. Notice how the backend intercepts XML `<function_call>` tags *while* streaming to the user, executes the search, and injects the results back into the LLM without breaking the connection.

```mermaid
sequenceDiagram
    participant Client as Frontend (React)
    participant API as FastAPI (api.py)
    participant Chat as ChatGPT (chat.py)
    participant LLM as Anthropic API
    participant Tools as Tool Handler

    Client->>API: POST /backend/chat (messages, snippets)
    API->>Chat: stream_state(messages)
    Chat->>LLM: messages.create(stream=True)
    
    loop Token Streaming
        LLM-->>Chat: yield token
        
        alt Token is standard text
            Chat-->>API: yield token
            API-->>Client: yield JSON Patch (UI updates)
        else Token contains <function_call>
            Note over Chat: Pauses stream to Client
            Chat->>Chat: Buffer tokens until </function_call>
            Chat->>Tools: handle_function_call(search_codebase, query)
            Tools->>Tools: Run Hybrid Search (Tantivy + Vector)
            Tools-->>Chat: Return new Snippets & <function_output>
            
            Chat-->>API: yield Tool Call UI State
            API-->>Client: yield JSON Patch (Shows "Searching...")
            
            Note over Chat: Injects <function_output> into LLM context
            Chat->>LLM: messages.create(append <function_output>)
        end
    end
    
    LLM-->>Chat: Final Answer Tokens
    Chat-->>API: yield tokens
    API-->>Client: yield JSON Patch (Final Markdown)
```

---

### 2. Frontend Architecture & Sequence

The frontend is a Next.js (App Router) application. It is designed as a real-time, reactive IDE. It uses `fast-json-patch` to handle streaming state updates efficiently and `CodeMirror` to render interactive code diffs.

#### Frontend Architecture Diagram

```mermaid
graph TD
    subgraph Next.js App Router
        Page["app/page.tsx (Entry)"]
        Auth["app/api/auth/[...nextauth]"]
    end

    subgraph Main Application State
        App["App.tsx (State Manager)"]
        StreamUtils["streamingUtils.ts (JSON Patching)"]
    end

    subgraph UI Components
        Sidebar["ContextSideBar.tsx"]
        SnippetSearch["SnippetSearch.tsx (Modal)"]
        MessageList["MessageDisplay.tsx"]
        CodeEditor["CodeMirrorSuggestionEditor.tsx"]
        PRDisplay["PullRequestDisplay.tsx"]
    end

    subgraph External/Backend
        FastAPI["FastAPI Backend"]
        GitHubOAuth["GitHub OAuth"]
    end

    %% Connections
    Page --> App
    Auth --> GitHubOAuth
    
    App --> Sidebar
    App --> MessageList
    App --> StreamUtils
    
    Sidebar --> SnippetSearch
    MessageList --> CodeEditor
    MessageList --> PRDisplay
    
    StreamUtils <-->|ReadableStream / JSON Patch| FastAPI
    SnippetSearch -->|Fetch Snippets| FastAPI
    CodeEditor -->|Apply Autofix| FastAPI
```

#### Frontend Sequence Diagram: Sending a Message & Rendering Diffs
This diagram shows what happens when a user types a message and hits "Send". It highlights the `fast-json-patch` streaming mechanism and how code suggestions are parsed and rendered into interactive CodeMirror editors.

```mermaid
sequenceDiagram
    participant User
    participant App as App.tsx
    participant Stream as streamingUtils.ts
    participant Backend as FastAPI
    participant MsgDisplay as MessageDisplay.tsx
    participant CodeMirror as CodeMirrorEditor

    User->>App: Types message & clicks "Send"
    App->>App: setMessages([...messages, new_message])
    App->>Backend: fetch('/backend/chat', {messages, snippets})
    
    Backend-->>Stream: ReadableStream (HTTP 200)
    
    loop Every Chunk received
        Stream->>Stream: getJSONPrefix(buffer)
        Stream-->>App: yield JSON Patch array (e.g., [{op: "add", path: "/1/content", value: "I found..."}])
        App->>App: jsonpatch.applyPatch(messages, patch)
        App->>MsgDisplay: Re-render with new message state
        
        alt Message contains <code_change> tags
            App->>App: extract_objects_from_string(code_change)
            App->>App: Update annotations.codeSuggestions state
            MsgDisplay->>CodeMirror: Render Original vs Modified Diff
            CodeMirror-->>User: Displays interactive side-by-side code
        end
    end
    
    Note over App: Stream completes
    App->>Backend: fetch('/backend/messages/save') (Debounced background save)
    
    opt User edits suggested code
        User->>CodeMirror: Types in editor
        CodeMirror->>App: setSuggestedChanges(newCode)
        User->>App: Clicks "Apply Changes"
        App->>Backend: fetch('/backend/autofix')
    end
```

### Key Architectural Takeaways:
1.  **JSON Patch Streaming:** Instead of sending the entire message string over and over (which is standard in basic ChatGPT clones), Sweep uses `fast-json-patch`. The backend calculates the diff of the state and streams only the patch. The frontend applies this patch to its local state. This is highly efficient for complex nested states (like tool calls and code suggestions).
2.  **Mid-Stream Interception:** The backend's ability to pause a stream, execute a tool, and resume the stream without the frontend having to orchestrate the tool call is a massive architectural advantage. It keeps the frontend "dumb" and the backend fully in control of the agentic loop.