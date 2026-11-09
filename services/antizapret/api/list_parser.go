package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path/filepath"
	"regexp"
	"strings"
)

// listParser streams normalized rules into the shared filtering/rendering path.
type listParser interface {
	Parse(io.Reader, func(listRule)) error
}

func (req ListRequest) parser(ctx context.Context, source string) listParser {
	switch req.Format {
	case "json":
		return jsonListParser{}
	case "auto", "v2fly":
		return &v2flyListParser{ctx: ctx, source: source, auto: req.Format == "auto", active: make(map[string]bool)}
	default:
		return plainListParser{preserve: req.Raw || req.Regex}
	}
}

type plainListParser struct{ preserve bool }

func (p plainListParser) Parse(reader io.Reader, emit func(listRule)) error {
	scanner := bufio.NewScanner(reader)
	for scanner.Scan() {
		line := scanner.Text()
		if !p.preserve {
			line = plainListLine(line)
		}
		emit(listRule{value: line})
	}
	return scanner.Err()
}

type jsonListParser struct{}

func (jsonListParser) Parse(reader io.Reader, emit func(listRule)) error {
	decoder := json.NewDecoder(reader)
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	if token != json.Delim('[') {
		return errors.New("expected JSON array")
	}
	for decoder.More() {
		var value string
		if err := decoder.Decode(&value); err != nil {
			return err
		}
		emit(listRule{value: value})
	}
	_, err = decoder.Token()
	return err
}

type v2flyRule struct {
	listRule
	attrs map[string]bool
}

type v2flyListParser struct {
	ctx    context.Context
	source string
	auto   bool
	files  int
	rules  int
	active map[string]bool
}

func (p *v2flyListParser) Parse(reader io.Reader, emit func(listRule)) error {
	return p.parseSource(p.source, reader, p.auto, func(rule v2flyRule) { emit(rule.listRule) })
}

// parseSource expands includes as they are read. Each include adds an attribute
// filter around emit, so neither parent nor child lists need buffering.
func (p *v2flyListParser) parseSource(source string, reader io.Reader, auto bool, emit func(v2flyRule)) error {
	if p.active[source] {
		return fmt.Errorf("cyclic v2fly include: %s", source)
	}
	if len(p.active) >= 32 || p.files >= 256 {
		return fmt.Errorf("v2fly include limit exceeded")
	}
	p.files++
	p.active[source] = true
	defer delete(p.active, source)
	if reader == nil {
		if strings.HasPrefix(source, "http://") || strings.HasPrefix(source, "https://") {
			request, err := http.NewRequestWithContext(p.ctx, http.MethodGet, source, nil)
			if err != nil {
				return err
			}
			response, err := listHTTPClient.Do(request)
			if err != nil {
				return err
			}
			defer response.Body.Close()
			if response.StatusCode != http.StatusOK {
				return fmt.Errorf("include %s returned %d", source, response.StatusCode)
			}
			reader = response.Body
		} else {
			file, err := openAllowedListFile(source)
			if err != nil {
				return err
			}
			defer file.Close()
			reader = file
		}
	}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	for line := 1; scanner.Scan(); line++ {
		text := scanner.Text()
		if auto && !isV2flyLine(text) {
			emit(v2flyRule{listRule: listRule{value: plainListLine(text)}})
			continue
		}
		text, _, _ = strings.Cut(text, "#")
		text = strings.TrimSpace(text)
		token := firstListWord(text)
		if token == "" {
			continue
		}
		kind, value, typed := strings.Cut(token, ":")
		if !typed {
			kind, value = "domain", kind
		}
		if value == "" {
			return fmt.Errorf("%s:%d: empty %s rule", source, line, kind)
		}
		var attrs map[string]bool
		for field := range strings.FieldsSeq(text[len(token):]) {
			switch {
			case strings.HasPrefix(field, "@") && len(field) > 1:
				if attrs == nil {
					attrs = make(map[string]bool)
				}
				attrs[field[1:]] = true
			case strings.HasPrefix(field, "&") && len(field) > 1:
				// Affiliations export this rule to another list; they do not change its match.
			default:
				return fmt.Errorf("%s:%d: invalid attribute %q", source, line, field)
			}
		}
		if kind == "include" {
			target, err := resolveV2flyInclude(source, value)
			if err != nil {
				return err
			}
			err = p.parseSource(target, nil, false, func(rule v2flyRule) {
				if rule.matches(attrs) {
					emit(rule)
				}
			})
			if err != nil {
				return err
			}
			continue
		}
		pattern := ""
		switch kind {
		case "domain":
			pattern = "||" + value + "^"
		case "full":
			pattern = "|" + value + "|"
		case "keyword":
			pattern = "/" + strings.ReplaceAll(regexp.QuoteMeta(value), "/", `\/`) + "/"
		case "regexp":
			if _, err := regexp.Compile(value); err != nil {
				return fmt.Errorf("%s:%d: %w", source, line, err)
			}
			pattern = "/" + strings.ReplaceAll(value, "/", `\/`) + "/"
		default:
			return fmt.Errorf("%s:%d: unsupported v2fly rule %q", source, line, kind)
		}
		p.rules++
		if p.rules > 1000000 {
			return fmt.Errorf("v2fly expanded rule limit exceeded")
		}
		emit(v2flyRule{listRule{value, pattern}, attrs})
	}
	return scanner.Err()
}

func resolveV2flyInclude(source, name string) (string, error) {
	ref, err := url.Parse(name)
	if err != nil {
		return "", err
	}
	// Absolute HTTP URLs are an adapter extension for composing lists from
	// different repositories. Standard v2fly includes remain sibling filenames.
	if ref.IsAbs() {
		if ref.Scheme != "http" && ref.Scheme != "https" {
			return "", fmt.Errorf("unsupported include scheme")
		}
		return ref.String(), nil
	}
	if name == "." || name == ".." || strings.ContainsAny(name, "/\\?#") {
		return "", fmt.Errorf("invalid include filename %q", name)
	}
	if strings.HasPrefix(source, "http://") || strings.HasPrefix(source, "https://") {
		base, err := url.Parse(source)
		if err != nil {
			return "", err
		}
		return base.ResolveReference(ref).String(), nil
	}
	return filepath.Join(filepath.Dir(source), name), nil
}

func (rule v2flyRule) matches(attrs map[string]bool) bool {
	for attr := range attrs {
		if rule.attrs[strings.TrimPrefix(attr, "-")] == strings.HasPrefix(attr, "-") {
			return false
		}
	}
	return true
}

func firstListWord(line string) string {
	for word := range strings.FieldsSeq(line) {
		return word
	}
	return ""
}

func plainListLine(line string) string {
	trimmed := strings.TrimSpace(line)
	if trimmed == "" {
		return line
	}
	switch trimmed[0] {
	case '/', '!', '#': // Regex rules and comments retain their contents.
		return line
	}
	return firstListWord(trimmed)
}

func isV2flyLine(line string) bool {
	line = strings.TrimSpace(line)
	kind, _, typed := strings.Cut(line, ":")
	if !typed {
		return false
	}
	switch kind {
	case "include", "domain", "full", "keyword", "regexp":
		return true
	}
	return false
}
