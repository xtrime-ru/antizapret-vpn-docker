BEGIN { RS="\n|\r" }

{
    sub(/#.*$/, "")
    sub(/^[ \t]+/, "")
    sub(/[ \t]+$/, "")
    if ($0 != "") print
}
