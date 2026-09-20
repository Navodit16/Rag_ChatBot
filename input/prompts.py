def get_image_extraction_prompt(person_name):
    return f"""Extract information from this image and respond ONLY with the single line below, filled in. No thinking, no explanation.

{{Name: {person_name}, Place: <concise merged summary of geo tag footer rows 1 & 2, remove duplicates, most specific address first e.g. "Hartirath Road, Alaipur, Varanasi, Uttar Pradesh 221001, India">, Date: <date from geo tag footer>, Time: <time from geo tag footer>, Any Doctor Name or Building Name Visible in background: <best effort, English only, or Not found>}}

Rules:
- Name is always: {person_name}
- For Place: merge both geo rows into one clean address, drop repeated parts, keep pincode if present
- Translate any non-English text to English
- Use "Not found" only if truly unreadable
- Output the single line. Nothing else."""

# """We already know the person's name is {Rahul}.
#  Please examine the image and return the information strictly in the VERBATIM format below.
#  If any of the ans cannot be given then the default ans should be "Not found". You must strictly keep the 'Name' as '{person_name}'.

#  {{Name: {person_name}, Place: (Summary of first two rows of the geo tag footer), Date: (from geo tag footer), Time: (from geo tag footer), Any Doctor Name or Building Name Visible in background: (answer, take your time here as the background can be blurry or pixelated. Translate to english if in any other language. Make no mistakes. Translate the answer to english if in any other language. Recheck your answer before returning.)}}"""


# """Extract information from this image and respond ONLY with the single line below, filled in. No thinking, no explanation.

# {{Name: {Rahul}, Place: <concise merged summary of geo tag footer rows 1 & 2, remove duplicates, most specific address first e.g. "Hartirath Road, Alaipur, Varanasi, Uttar Pradesh 221001, India">, Date: <date from geo tag footer>, Time: <time from geo tag footer>, Any Doctor Name or Building Name Visible in background: <best effort, English only, or Not found>}}

# Rules:
# - Name is always: {Rahul}
# - For Place: merge both geo rows into one clean address, drop repeated parts, keep pincode if present
# - Translate any non-English text to English
# - Use "Not found" only if truly unreadable
# - Output the single line. Nothing else.""" Best till now