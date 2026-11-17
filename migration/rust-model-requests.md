# Approved native model request bodies

Only the five application-owned contracts can supply external schema
metadata. The checked-in schema documents come from the pinned Python
classes. The builder takes the model name and output limit from configuration,
keeps the instruction and prompt in their existing fields, and preserves
OpenAI's storage opt-out. The response uses the same wire and application
contracts that were prepared for the request.

Ten complete request bodies matched the existing adapters using local HTTP
mocks, including the field order in the serialized schema. The fixture also
checked invalid configuration and rejection of a
response outside the selected contract. Provider transport and application
command validation are separate.
