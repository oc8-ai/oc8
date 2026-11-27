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
//   prefetch switch, and <base>.
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
// | innerHTML / outerHTML (Element, ShadowRoot),         | the changed subtree swept  |
// |   insertAdjacentHTML                                 | before the call returns    |
// | createElement / createElementNS                      | a frame-ish name throws    |
// | setAttribute(NS), toggleAttribute, setAttributeNode  | the element removed before |
// |   (NS), Attr.value, link.rel/href,                   | the call returns if it is  |
// |   meta.httpEquiv/content                             | now forbidden              |
// | NamedNodeMap.setNamedItem(NS)                        | page swept after (sync)    |
// | link.relList (DOMTokenList, shared with classList)   | observer (attributes)      |
// | document.write/writeln/open, execCommand,            | removed (they parse        |
// |   setHTMLUnsafe, parseHTMLUnsafe, setHTML,           |   declarative shadow roots |
// |   attachShadow                                       |   or hide nodes in shadow  |
// |                                                      |   trees the sweep can't    |
// |                                                      |   see)                     |
// | DOMParser, createContextualFragment, importNode,     | nothing to do: the result  |
// |   adoptNode, cloneNode, template.content,            |   is detached until one of |
// |   createHTMLDocument, XSLTProcessor                  |   the insertions above     |
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
  const listLength = get(NodeList, "length");
  const localName = get(Element, "localName");
  const ownerElement = get(Attr, "ownerElement");
  const matches = fn(Element, "matches");
  const elementAll = fn(Element, "querySelectorAll");
  const fragmentAll = fn(DocumentFragment, "querySelectorAll");
  const documentAll = fn(Document, "querySelectorAll");
  const Observer = MutationObserver;
  const observe = fn(MutationObserver, "observe");
  const Blocked = TypeError;
  const keep = apply(fn(Document, "querySelector"), doc, ['meta[http-equiv="x-dns-prefetch-control"]']);

  const typeOf = (node) => { try { return apply(nodeType, node, []); } catch { return 0; } };
  const removeNode = (node) => {
    if (node === keep) return;
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
  // Inserts the nodes at the argument index \`at\` (null: every argument).
  const inserting = (at) => (original) => function (...args) {
    for (let i = 0; i < args.length; i += 1) if (at === null || at === i) scrub(args[i]);
    return apply(original, this, args);
  };
  // Changes an element's attributes: if that makes it forbidden, it goes.
  const attributing = (elementOf) => (original) => function (...args) {
    try { return apply(original, this, args); } finally {
      const element = elementOf(this);
      if (forbidden(element)) removeNode(element);
    }
  };
  const self = (node) => node;
  const owner = (attr) => { try { return apply(ownerElement, attr, []); } catch { return null; } };
  // Parses HTML into \`root\`: swept before the call returns.
  const parsing = (rootOf) => (original) => function (...args) {
    const root = rootOf(this);
    try { return apply(original, this, args); } finally { sweep(root); }
  };
  const parentOr = (node) => { try { return apply(parentNode, node, []) || node; } catch { return node; } };

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

  setter(Element, "innerHTML", parsing(self));
  setter(Element, "outerHTML", parsing(parentOr));
  method(Element, "insertAdjacentHTML", parsing(parentOr));
  if (typeof ShadowRoot !== "undefined") setter(ShadowRoot, "innerHTML", parsing(self));

  for (const name of ["setAttribute", "setAttributeNS", "toggleAttribute", "setAttributeNode", "setAttributeNodeNS"]) {
    method(Element, name, attributing(self));
  }
  setter(Attr, "value", attributing(owner));
  setter(HTMLLinkElement, "rel", attributing(self));
  setter(HTMLLinkElement, "href", attributing(self));
  setter(HTMLMetaElement, "httpEquiv", attributing(self));
  setter(HTMLMetaElement, "content", attributing(self));
  method(NamedNodeMap, "setNamedItem", parsing(() => doc));
  method(NamedNodeMap, "setNamedItemNS", parsing(() => doc));

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

  // The backstop: whatever changed the page by another path, swept in the
  // next microtask -- before any nested document could load.
  apply(observe, new Observer(sweepAll), [doc, {
    childList: true, subtree: true, attributes: true,
    attributeFilter: ["rel", "href", "http-equiv", "content"],
  }]);
})();`;
