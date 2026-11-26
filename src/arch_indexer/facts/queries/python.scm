; Tier 2 fact captures for Python. The only per-language part of the extractor besides
; the argument classifier in extract.py. Comments and string literals are never matched
; because they contain no ``call``/``decorator`` nodes of their own.
(call
  function: (_) @callee
  arguments: (_) @args) @call

(decorated_definition
  (decorator (_) @decorator) @decorator_node
  definition: (_) @target)
