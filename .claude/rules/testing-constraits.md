# Testing constraints
All features should be tested by agents. 
- If it has a UI and it can be tested from the user interface, then `nightmare tester` should test both logic and the UI
- If it doesn't have a UI, like a new dockerfile, a new service, a new logic through a specific action, then `logic tester` agent should test it through django commands, normal shell commands, or any other ways
- The test result should be passed to me for final review, what tested, what was the errors, and what fixed
**The point is, all features should be tested by agents, and all the requested requirements SHOULD BE READY AND E2E tested by agents**