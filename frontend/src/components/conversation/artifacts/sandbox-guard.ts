// GUARD: the first script of every scripted preview frame (see
// sandbox-srcdoc.ts). A hardening layer over the opaque origin and the CSPs,
// not a boundary of its own: it runs in the same realm as the page it guards.
// What it is for:
// - WebRTC: no browser we test (Chromium, Firefox) applies CSP to it, so the
//   page loses RTCPeerConnection & co. here, together with every other way to
//   send (fetch, XHR, WebSocket, EventSource, WebTransport, sendBeacon), which
//   the CSP blocks anyway.
// - A fresh realm: the only way to globals GUARD has not touched is a nested
//   browsing context. A nested about:blank is cross-origin in a sandbox (its
//   own opaque origin), but a nested srcdoc runs its own inline script there.
//   So no frame-ish element (iframe, frame, object, embed, portal,
//   fencedframe) may stay connected until its document loads (a later task).
// - Hosts reached outside CSP: resource hints (dns-prefetch, preconnect, and
//   the rest of the hint family) and a late <meta http-equiv> refresh or DNS
//   prefetch switch, and <base>. The srcdoc's own `x-dns-prefetch-control:
//   off` meta goes too, at once: the switch is read when the meta is parsed
//   and a "off" is final in Chromium and Firefox, so the element is not
//   needed -- and left in place it could be rewritten into a refresh.
//
// Against a page that pollutes prototypes, GUARD uses only what it captured
// before the page ran (Reflect.apply, property descriptors, the DOM methods
// and getters it needs, the MutationObserver), iterates with index loops on
// static NodeLists (indices are own properties of the list, not overridable
// on the prototype), and installs every patched member non-writable and
// non-configurable.
//
// Where a node can be connected or changed, and what GUARD does there:
//
// | Entry point                                          | GUARD                      |
// |------------------------------------------------------|----------------------------|
// | initial markup (the page itself)                     | stripped in the parent     |
// |                                                      | (withoutNetworkHints); the |
// |                                                      | observer as backstop       |
// | Node: appendChild, insertBefore, replaceChild,       | node scrubbed before:      |
// |   moveBefore                                         | forbidden descendants go,  |
// |                                                      | a forbidden node throws    |
// | append, prepend, before, after, replaceWith,         | same                       |
// |   replaceChildren (Element, Document, Fragment,      |                            |
// |   CharacterData, DocumentType)                       |                            |
// | Element.insertAdjacentElement, Range.insertNode,     | same                       |
// |   Range.surroundContents, document.body setter       |                            |
// | innerHTML / outerHTML (Element, ShadowRoot),         | parsed into a <template>   |
// |   insertAdjacentHTML                                 | first: forbidden throws;   |
// |                                                      | then the changed subtree   |
// |                                                      | swept before returning (a  |
// |                                                      | context that parses        |
// |                                                      | differently, e.g. <svg>)   |
// | createElement / createElementNS                      | a frame-ish name throws    |
// | setAttribute(NS), toggleAttribute, setAttributeNode  | tried on a detached copy   |
// |   (NS), link.rel/href, meta.httpEquiv/content        | first; a change that would |
// |                                                      | make it forbidden is       |
// |                                                      | ignored (no-op)            |
// | an attribute node: Attr.value, Node.nodeValue,       | same, on its owner element |
// |   Node.textContent                                   |                            |
// | NamedNodeMap.setNamedItem(NS) (element.attributes)   | same, on the map's element |
// | link.relList: DOMTokenList add, toggle, replace,     | same, on the list's link   |
// |   value (the prototype is shared with classList:     | (lists GUARD saw handed    |
// |   only lists handed out by relList are checked)      | out by relList)            |
// | document.write/writeln/open, execCommand,            | removed (they parse        |
// |   setHTMLUnsafe, parseHTMLUnsafe, setHTML,           |   declarative shadow roots |
// |   attachShadow                                       |   or hide nodes in shadow  |
// |                                                      |   trees the sweep can't    |
// |                                                      |   see)                     |
// | DOMParser, createContextualFragment, importNode,     | nothing to do: the result  |
// |   adoptNode, cloneNode, template.content,            |   is detached until one of |
// |   createHTMLDocument, XSLTProcessor                  |   the insertions above     |
// | location = "javascript:…", a javascript: link        | not GUARD: the opaque-     |
// |                                                      |   origin sandbox does not  |
// |                                                      |   run them (with           |
// |                                                      |   allow-same-origin it     |
// |                                                      |   would, in a fresh realm) |
// | anything else                                        | the observer: a sweep in   |
// |                                                      |   the next microtask       |
export const GUARD = `(() => {
  "use strict";
  const R = Reflect, apply = R.apply, own = R.getOwnPropertyDescriptor, define = R.defineProperty;
  const doc = document;
  const P = (C) => C && C.prototype;
  const get = (C, name) => { const d = own(P(C), name); return d && d.get; };
  const fn = (C, name) => { const d = own(P(C), name); return d && d.value; };

  const FORBIDDEN =
    "iframe,frame,object,embed,portal,fencedframe,base," +
    'link[rel*="prefetch" i],link[rel*="preconnect" i],link[rel*="prerender" i],link[rel*="preload" i],' +
    'meta[http-equiv*="refresh" i],meta[http-equiv*="dns-prefetch" i]';

  const nodeType = get(Node, "nodeType");
  const parentNode = get(Node, "parentNode");
  const removeChild = fn(Node, "removeChild");
  const cloneNode = fn(Node, "cloneNode");
  const listLength = get(NodeList, "length");
  const localName = get(Element, "localName");
  const setAttributeNS = fn(Element, "setAttributeNS");
  const attrName = get(Attr, "name");
  const attrValue = get(Attr, "value");
  const attrNamespace = get(Attr, "namespaceURI");
  const ownerElement = get(Attr, "ownerElement");
  const matches = fn(Element, "matches");
  const elementAll = fn(Element, "querySelectorAll");
  const fragmentAll = fn(DocumentFragment, "querySelectorAll");
  const documentAll = fn(Document, "querySelectorAll");
  const relList = get(HTMLLinkElement, "relList");
  const Observer = MutationObserver;
  const observe = fn(MutationObserver, "observe");
  const Blocked = TypeError;
  // DOMTokenLists (link.relList) and NamedNodeMaps (element.attributes) GUARD
  // handed out, to the element they belong to.
  const owners = new WeakMap();
  const ownerGet = fn(WeakMap, "get");
  const ownerSet = fn(WeakMap, "set");
  const ownerOf = (x) => { try { return apply(ownerGet, owners, [x]); } catch { return undefined; } };
  // An inert place to parse HTML before it goes into the page.
  const template = apply(fn(Document, "createElement"), doc, ["template"]);
  const innerHTMLSet = own(P(Element), "innerHTML").set;
  const templateContent = get(HTMLTemplateElement, "content");

  const typeOf = (node) => { try { return apply(nodeType, node, []); } catch { return 0; } };
  const removeNode = (node) => {
    const parent = apply(parentNode, node, []);
    if (parent) apply(removeChild, parent, [node]);
  };
  const drop = (list) => {
    for (let i = 0, n = apply(listLength, list, []); i < n; i += 1) removeNode(list[i]);
  };
  const forbidden = (node) => typeOf(node) === 1 && apply(matches, node, [FORBIDDEN]);
  // Everything forbidden in \`root\` (an element, a fragment, the document) goes.
  const sweep = (root) => {
    const type = typeOf(root);
    if (type === 1) {
      drop(apply(elementAll, root, [FORBIDDEN]));
      if (forbidden(root)) removeNode(root);
    } else if (type === 11) drop(apply(fragmentAll, root, [FORBIDDEN]));
    else drop(apply(documentAll, doc, [FORBIDDEN]));
  };
  const sweepAll = () => sweep(doc);
  // A node about to be inserted: its forbidden descendants go; a forbidden
  // node itself is refused.
  const scrub = (node) => {
    if (forbidden(node)) throw new Blocked("blocked in a preview");
    const type = typeOf(node);
    if (type === 1 || type === 11) sweep(node);
  };
  // HTML about to be parsed into the page: refused if it holds anything
  // forbidden, parsed in an inert template.
  const vet = (html) => {
    apply(innerHTMLSet, template, [html]);
    const found = apply(listLength, apply(fragmentAll, apply(templateContent, template, []), [FORBIDDEN]), []);
    apply(innerHTMLSet, template, [""]);
    if (found) throw new Blocked("blocked in a preview");
  };
  // Whether \`change\` would make \`element\` forbidden: tried on a detached
  // copy, which the browser does not act on.
  const wouldForbid = (element, change) => {
    if (typeOf(element) !== 1) return false;
    let copy;
    try { copy = apply(cloneNode, element, [false]); change(copy); } catch { return false; }
    return forbidden(copy);
  };
  const str = (v) => \`\${v}\`;
  const nullableStr = (v) => (v === null || v === undefined ? "" : \`\${v}\`);

  const lock = (target, name, descriptor) => {
    try { define(target, name, { ...descriptor, configurable: false }); } catch {}
  };
  const gone = (target, name) => {
    if (target) lock(target, name, { value: undefined, writable: false, enumerable: false });
  };
  const method = (C, name, wrap) => {
    const original = fn(C, name);
    if (typeof original === "function") {
      lock(P(C), name, { value: wrap(original), writable: false, enumerable: true });
    }
  };
  const setter = (C, name, wrap) => {
    const d = own(P(C), name);
    if (d && d.set) lock(P(C), name, { get: d.get, set: wrap(d.set), enumerable: d.enumerable });
  };
  const getter = (C, name, wrap) => {
    const d = own(P(C), name);
    if (d && d.get) lock(P(C), name, { get: wrap(d.get), set: d.set, enumerable: d.enumerable });
  };
  // Inserts the nodes at the argument index \`at\` (null: every argument).
  const inserting = (at) => (original) => function (...args) {
    for (let i = 0; i < args.length; i += 1) if (at === null || at === i) scrub(args[i]);
    return apply(original, this, args);
  };
  // Parses HTML (argument \`at\`) into \`root\`: vetted before, swept after.
  const parsing = (at, rootOf) => (original) => function (...args) {
    args[at] = str(args[at]);
    vet(args[at]);
    const root = rootOf(this);
    try { return apply(original, this, args); } finally { sweep(root); }
  };
  const self = (node) => node;
  const parentOr = (node) => { try { return apply(parentNode, node, []) || node; } catch { return node; } };
  // Changes an attribute of \`elementOf(this)\`: \`args\` made strings by
  // \`normalize\`, then tried on a copy by \`simulate(copy, args)\`; a change
  // that would make the element forbidden does nothing.
  const attributing = (elementOf, normalize, simulate) => (original) => function (...args) {
    const element = elementOf(this);
    if (element) {
      args = normalize(args);
      if (wouldForbid(element, (copy) => simulate(original, copy, args))) return undefined;
    }
    return apply(original, this, args);
  };
  const same = (original, copy, args) => apply(original, copy, args);
  const viaAttr = (attr) => (copy) =>
    apply(setAttributeNS, copy, [apply(attrNamespace, attr, []), apply(attrName, attr, []), apply(attrValue, attr, [])]);
  const element = (x) => (typeOf(x) === 1 ? x : null);
  // The element an attribute node belongs to (null for any other node).
  const attrOwner = (node) => {
    if (typeOf(node) !== 2) return null;
    try { return apply(ownerElement, node, []); } catch { return null; }
  };
  // An attribute node given a new value, tried as that attribute on a copy.
  const attrValueTo = (nullable) => (original) => function (...args) {
    const owner = attrOwner(this);
    if (owner) {
      const value = nullable ? nullableStr(args[0]) : str(args[0]);
      const attr = this;
      const ns = apply(attrNamespace, attr, []), name = apply(attrName, attr, []);
      if (wouldForbid(owner, (copy) => apply(setAttributeNS, copy, [ns, name, value]))) return undefined;
      return apply(original, this, [value]);
    }
    return apply(original, this, args);
  };

  for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel",
    "RTCIceTransport", "RTCSctpTransport", "WebSocket", "WebSocketStream", "WebTransport",
    "EventSource", "fetch", "XMLHttpRequest"]) {
    gone(window, name);
  }
  gone(P(Navigator), "sendBeacon");
  gone(navigator, "sendBeacon");
  for (const name of ["write", "writeln", "open", "execCommand"]) gone(P(Document), name);
  gone(Document, "parseHTMLUnsafe");
  for (const name of ["attachShadow", "setHTMLUnsafe", "setHTML"]) gone(P(Element), name);
  if (typeof ShadowRoot !== "undefined") {
    gone(P(ShadowRoot), "setHTMLUnsafe");
    gone(P(ShadowRoot), "setHTML");
  }

  for (const name of ["appendChild", "insertBefore", "replaceChild", "moveBefore"]) {
    method(Node, name, inserting(0));
  }
  for (const C of [Element, Document, DocumentFragment, CharacterData, DocumentType]) {
    for (const name of ["append", "prepend", "before", "after", "replaceWith", "replaceChildren"]) {
      if (own(P(C), name)) method(C, name, inserting(null));
    }
  }
  method(Element, "insertAdjacentElement", inserting(1));
  method(Range, "insertNode", inserting(0));
  method(Range, "surroundContents", inserting(0));
  setter(Document, "body", inserting(0));

  setter(Element, "innerHTML", parsing(0, self));
  setter(Element, "outerHTML", parsing(0, parentOr));
  method(Element, "insertAdjacentHTML", parsing(1, parentOr));
  if (typeof ShadowRoot !== "undefined") setter(ShadowRoot, "innerHTML", parsing(0, self));

  method(Element, "setAttribute", attributing(element, (a) => [str(a[0]), str(a[1])], same));
  method(Element, "setAttributeNS", attributing(element,
    (a) => [a[0] === null || a[0] === undefined ? null : str(a[0]), str(a[1]), str(a[2])], same));
  method(Element, "toggleAttribute", attributing(element, (a) => [str(a[0]), ...a.slice(1)], same));
  for (const name of ["setAttributeNode", "setAttributeNodeNS"]) {
    method(Element, name, attributing(element, (a) => a, (original, copy, args) => viaAttr(args[0])(copy)));
  }
  for (const [C, name] of [[HTMLLinkElement, "rel"], [HTMLLinkElement, "href"],
    [HTMLMetaElement, "httpEquiv"], [HTMLMetaElement, "content"]]) {
    setter(C, name, attributing(element, (a) => [str(a[0])], same));
  }
  setter(Attr, "value", attrValueTo(false));
  setter(Node, "nodeValue", attrValueTo(true));
  setter(Node, "textContent", attrValueTo(true));

  // element.attributes and link.relList: remember whose they are.
  const handedOut = (original) => function () {
    const value = apply(original, this, []);
    try { apply(ownerSet, owners, [value, this]); } catch {}
    return value;
  };
  getter(Element, "attributes", handedOut);
  getter(HTMLLinkElement, "relList", handedOut);
  for (const name of ["setNamedItem", "setNamedItemNS"]) {
    method(NamedNodeMap, name, attributing(ownerOf, (a) => a, (original, copy, args) => viaAttr(args[0])(copy)));
  }
  const onList = (original, copy, args) => apply(original, apply(relList, copy, []), args);
  const isLinkList = (list) => { const owner = ownerOf(list); return owner && typeOf(owner) === 1 ? owner : null; };
  method(DOMTokenList, "add", attributing(isLinkList, (a) => a.map(str), onList));
  method(DOMTokenList, "toggle", attributing(isLinkList, (a) => [str(a[0]), ...a.slice(1)], onList));
  method(DOMTokenList, "replace", attributing(isLinkList, (a) => [str(a[0]), str(a[1])], onList));
  setter(DOMTokenList, "value", attributing(isLinkList, (a) => [str(a[0])], onList));

  // Creating a frame-ish element fails outright (the insertions above are
  // what makes this hold; this only fails early).
  for (const name of ["createElement", "createElementNS"]) {
    method(Document, name, (original) => function (...args) {
      const element = apply(original, this, args);
      const local = apply(localName, element, []);
      if (local === "iframe" || local === "frame" || local === "object" || local === "embed" ||
          local === "portal" || local === "fencedframe") {
        throw new Blocked("blocked in a preview");
      }
      return element;
    });
  }

  // The srcdoc's own prefetch switch has done its work (see above).
  sweepAll();
  // The backstop: whatever changed the page by another path, swept in the
  // next microtask -- before any nested document could load.
  apply(observe, new Observer(sweepAll), [doc, {
    childList: true, subtree: true, attributes: true,
    attributeFilter: ["rel", "href", "http-equiv", "content"],
  }]);
})();`;
