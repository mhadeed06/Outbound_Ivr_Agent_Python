import asyncio
import httpx
import time

# 1) Your IVR prompt template
PROMPT_TEMPLATE = """

Your job is to process IVR system prompts during an interactive phone call.  


*********
Allowed Response Formats:
- dtmf:<digit> → press a keypad digit
- say:<phrase> → speak a word or phrase aloud
- value:<value> → provide a Member ID, Date of Birth, or NPI
- confirm:<yes/no> → confirm a heard value
- endcall → terminate the call
***********
**Rules**: Only use one of the above formats—no extra text.
DONOT RESPOSEN IF THE IVE DOESNOT ASK FOR SOMETHING EXPLICITLY, LIKE "PLEASE SAY YOUR NPI" OR "PLEASE PRESS 1 FOR CLAIMS"
"If you think the text is incomplete, wait for the next prompt to complete before responding. adn send fallback"


Call Information
***
NPI: 1912659939  spoken OR DMTF
Member ID: "U seven zero two three nine six five three zero one" (Spoken)    
Member Name: Thompson family health and Wellness PLLC (**Important note**: Some time the system won't say the last name, so just say SOMETHING LIKE IT , or just say NAME WITH SPELLING MISTAKE, or something matching, so validate that name and confirm it)
DOS: 06/02/2025
***

### 🗂️ Call Flow Outline
 
STEP 1: Caller Type Selection
    IVR PROMPT: If you know the extension of the agent you'd like to reach, pleae say extension
    OUR RESPOSNE: say provider   OR press 2: dtmf:2
    ######   OR    #####
    IVR PROMPT: IF THE IVE ASKS "IF YOU ARE A PROVIDER OR MEMBER OR DOCTOR OR SOMETING ELSE"?
    OUR RESPOSNE: say provider or press 2: dtmf:2
 
STEP 2: Action Menu
    "ARE YOU QUESTION TODAY ABOUT CLAIMS, ELIGIBILITY, OR AUTHORIZATION?"
    "IT WILL SAY I KNOW YOU ARE A PROVIDER, IN A FEW WORDS PLEASE STATE THE REASON FOR YOUR CALL, IT WILL GIVE OPTIONS LIKE CLAIMS ELIGIBILITY AND AUTHORIZATION"
    WE ALWAYS HAVE TO CHOOSE CLAIMS, by pressing 1: dtmf:1  OR saying claims

 
STEP 3: NPI
    Please say I'm ready or press 1 when you have it.
    **WHEN THE IVR SAYS ****PLEASE SAY I AM READY, ONLY THEN SAY I AM READY, DONOT RESPOSND THIS WITH ANYOTHER IVR PROMPT"***
    
    Then when the bot confirms back the NPI match it to our number and then response confirm:yes

    ""THEN IT WILL ASK ARE YOU CALLING ON BEHALF OF AND TELL SOME NAME, YOU WILL ALWAYS SAY YES TO THE NAME THING, DOES NOT MATTER WHAT THE NAME IS, JUST SAY YES TO THE NAME THING, LIKE THIS: confirm:yes ""

 
STEP 4: Type of claim
    "It speaks a random info line then ask for the type of claim, do not end the call, just wait for the next prompt"
    "First hear the options and then respond accordingly, if it asks for the type of claim, choose medical, and there should be an option of medical"
    "it will ask for the type of claim, choose medical and we can choose medical by saying say:medical or pressing 1: dtmf:1"


STEP 5: Options to search claim (DOS)
    "It will ask how to search your claim, always choose date of service(DOS)" we will do this by saying DATE OF SERVICE or pressing 2: dtmf:2
    And then they will ask for the DOS provide the date of service, provide in the format MM-DD-YYYY: value:06/02/2025

   

STEP 6: MEMBER ID
    "If IVR asks for Member ID, provide it: value:U seven zero two three nine six five three zero one"
    Always speak member id very slowly, like this: say:U seven zero two three nine six five three zero one


STEP 7: Final Response
    Then there will be 2 options, it may give you the details of the claim or say like i don't find anything, or like that
    In both cases end the call with endcall.

 
8. **After you get the answer, simply end the call**
    ### 🚨 Important Rules
    - Please end the call when the IVR says something like "Looks like you're having trouble. Let's connect you to the agent."
    then hung up the call with endcall.
    ---

Now, read the following IVR prompt and reply accordingly using the correct format only:
Process this prompt and don't press any key until you find an explicit instruction to respond.
******  IMPORTAN RULESS ******
**If you think the IVR PROMPT is incomplete or it doesnot explicitly says to do something , please return fallback**
**IF THE IVR MSG IS SOMETHING HICH IS NOT MENTIONED IN THE ABOVE STEPS, THEN JUST RETURN Fallback**


IVR Message: "{transcript}"
""".strip()



# 2) Edit this variable with whatever test transcript you like:
TRANSCRIPT = "to proceed we require NPI we will give you a moment to locate your information"


#  This call will be recorded for quality and training purposes to continue in English or if you are a provider say English or press 1
#To continue in any other language
#If you know the extension of the agent you'd like to read
#Are your questions today around eligibility claim status or prior authorizations
#with a vanity you can save time and get many of these answers via our secure portaloravailability.com check out availity.com I see you are a provider in a few words please state the reason for your call for example you can say things like claims eligibility or authorization
#Let's try again I see you are a provider in a few words please state the reason for your call for example you can say things like claims eligibility or authorization
#Great let's get you connected
#to proceed we require NPI we will give you a moment to locate your information please say I'm ready or press 1 when you have it
#Please say your NPI
#I heard
#1912659939 is this correct
#one moment while I look that up
#are you calling on behalf
#Are you calling on behalf of Thompson family health and Wellness PLLC you can say yes or press 1 you can also say no or press 2
#with a vanity you can save time and get many of these answers via our secure.portal.or@availity.com check out availity.com I see you are a provider in a few words please state the reason for your call for example you can say things like claims eligibility or authorization




# 3) The function that actually calls your Llama API
async def call_llama_api(prompt: str) -> str:
    url = "http://20.172.5.137:9010/api/generate_response/"
    payload = {
        "doctor_query": prompt,
        "role": "You are an outbound calling agent for insurance IVR handling claim status calls.",
        "max_new_tokens": 100
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
        data = resp.json()
        if resp.status_code == 200:
            return data.get("response", "(no response)")
        return f"Error {resp.status_code}: {data!r}"

# 4) Glue it together in a main()
async def main():
    # Build the full prompt
    prompt = PROMPT_TEMPLATE.format(transcript=TRANSCRIPT)

    # Send to Llama and measure latency
    t0 = time.perf_counter()
    response = await call_llama_api(prompt)
    ms = (time.perf_counter() - t0) * 1000

    print("=== Llama Response ===")
    print(response)
    print(f"\n⏱️  Latency: {ms:.0f} ms")

if __name__ == "__main__":
    asyncio.run(main())




