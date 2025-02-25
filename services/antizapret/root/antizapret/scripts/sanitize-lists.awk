BEGIN {
    RS="\n|\r";
    IGNORECASE=1;
}

# Remove comments
{sub(/#.*$/, "", $1)}

#Strip spaces
{sub(/\s/, "", $1)}

# Skipping empty strings
(!$1) {next}

{print $1}
