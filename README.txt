feature/projects-m0-5-put-chats-in-projects @ 9451e1e, exported 2026-10-12T20:03:50
Base: projects-dev @ 6ef8e3e Merge branch 'feature/projects-m0-4-list-project-chats' into projects-dev

Pull request ---------------------------------------------------------------
Branch: feature/projects-m0-5-put-chats-in-projects
Commit: 9451e1e7b8e6f2f7a80eadb83bd6ef13de98a29d

Title:
feat(conversations): start chats in projects and move chats into them

Description:
- `POST /chatbot/streaming/conversations` with `project_id`:
  - creates the chat in that project
  - the project is checked before the stream starts: an unknown or non-UUID id gives 404 `{"detail": "Project not found."}`, another user's project gives 403 `FORBIDDEN_PROJECT_ACCESS`
  - a blank or null `project_id` creates the chat outside projects
- `PATCH /chatbot/conversations/{id}` with `{"project_id": "<id>"}`:
  - moves the chat into that project, also from another project
  - an unknown or non-UUID id gives 404, another user's project gives 403 `FORBIDDEN_PROJECT_ACCESS`, another user's chat gives 403 `FORBIDDEN_CONVERSATION_ACCESS`
  - `null` or blank gives 400
  - the chat's `updated_at` is unchanged
- `find_my_project` in `project_views.py`, used by the conversation list, the conversation patch and the streaming create
- `move_conversation_to_project` on `WebConversationService` and `WebConversationRepository`; `create_conversation` takes `project_id`
- Swagger: 403 and 404 on the streaming create and the conversation patch; the "accepted but not applied yet" notes are gone

🤖 Generated with [Claude Code](https://claude.com/claude-code)
----------------------------------------------------------------------------

On the company laptop, from this folder:
  python3 apply.py /path/to/twin --dry-run   # show what would change
  python3 apply.py /path/to/twin             # apply it (asks first)

files/  added and modified files, exactly as committed on the branch
base/   the version each modified file had when the branch started; apply.py
        merges with it when the company copy changed since. Don't edit.

Commits:
  9451e1e feat(conversations): start chats in projects and move chats into them

Files (A added, M modified, D deleted):
  M src/chatbot/serializers.py
  M src/chatbot/views/chatbot_views.py
  M src/chatbot/views/project_views.py
  M src/chatbot/views/stream_views.py
  M src/chatbot/web_conversation_repository.py
  M src/chatbot/web_conversation_service.py
  M tests/unit/chatbot/test_web_conversation_service.py
  M tests/unit/chatbot/views/test_conversation_views.py
  A http/projects.http
