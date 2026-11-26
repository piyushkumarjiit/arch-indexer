; Tier 2 fact captures for TypeScript/JavaScript (TSX shares the same node names).
(call_expression
  function: (_) @callee
  arguments: (arguments) @args) @call

(new_expression
  constructor: (_) @callee
  arguments: (arguments) @args) @call

(decorator (_) @decorator) @decorator_node
