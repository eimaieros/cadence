"""Retrieval over the job description a candidate pastes in.

The interviewer is better when it knows what the job actually asks for. The
naive way to give it that is to paste the whole description into the system
prompt. That is the one place this codebase never puts user text: a job ad is
written by somebody else, copied by the candidate, and can carry instructions
just as easily as an answer can.

So the description is split into passages (:mod:`.chunking`), each passage is
embedded (:mod:`.embedding`), and the interviewer gets a tool that searches
them. What comes back is a tool result -- data, handled with the same suspicion
as an answer -- and only the few passages that matter for the next question,
rather than the whole ad on every turn.
"""
