; Tier 2 fact captures for TypeScript/JavaScript (TSX shares the same node names).
(call_expression
  function: (_) @callee
  arguments: (arguments) @args) @call

(new_expression
  constructor: (_) @callee
  arguments: (arguments) @args) @call

(decorator (_) @decorator) @decorator_node

(binary_expression
  left: (_) @lhs
  operator: _ @op
  right: (_) @literal) @compare

(call_expression
  function: (member_expression object: (_) @lhs property: (property_identifier) @op)
  arguments: (arguments (_) @literal .)) @compare
